# -*- coding: utf-8 -*-
import ctypes
import cv2
from ..configure import boxcfg
from ..configure.config import rootdir
from ctypes.util import find_library
import asyncio
import copy
import re
import shutil
import subprocess
import sys
import threading
import time

import speech_recognition as sr
import os

import whisper
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QMessageBox
from pydub import AudioSegment
from pydub.silence import detect_nonsilent
import srt
from datetime import timedelta
import json
import edge_tts
import textwrap

from ..translator import baidutrans, googletrans, tencenttrans, chatgpttrans, deepltrans, deeplxtrans, baidutrans_spider

from videotrans.configure import config
from videotrans.configure.config import logger, transobj, queue_logs

# 获取代理，如果已设置os.environ代理，则返回该代理值,否则获取系统代理
from ..tts import get_voice_openaitts, get_voice_edgetts

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
else:
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())


def transcribe_audio(audio_path, model, language):
    model = whisper.load_model(model, download_root=rootdir + "/models")  # Change this to your desired model
    transcribe = model.transcribe(audio_path, language="zh" if language in ["zh-cn", "zh-tw"] else language)
    segments = transcribe['segments']
    result = ""
    for segment in segments:
        startTime = str(0) + str(timedelta(seconds=int(segment['start']))) + ',000'
        endTime = str(0) + str(timedelta(seconds=int(segment['end']))) + ',000'
        text = segment['text']
        segmentId = segment['id'] + 1
        result += f"{segmentId}\n{startTime} --> {endTime}\n{text.strip()}\n\n"
    return result


# 获取摄像头
def get_camera_list():
    if boxcfg.check_camera_ing:
        return
    boxcfg.check_camera_ing = True
    index = 0
    if len(boxcfg.camera_list) > 0:
        boxcfg.check_camera_ing = False
        return
    print("获取摄像头")
    try:
        while True:
            camera = cv2.VideoCapture(index)
            if not camera.read()[0]:
                break
            else:
                boxcfg.camera_list.append(index)
                index += 1
        camera.release()
        print(f"{boxcfg.camera_list=}")
    except Exception as e:
        print("获取摄像头出错")
    boxcfg.check_camera_ing = False


def find_lib():
    dll = None
    plugin_path = os.environ.get('PYTHON_VLC_MODULE_PATH', None)
    if 'PYTHON_VLC_LIB_PATH' in os.environ:
        try:
            dll = ctypes.CDLL(os.environ['PYTHON_VLC_LIB_PATH'])
        except OSError:
            return
    if plugin_path and not os.path.isdir(plugin_path):
        return
    if dll is not None:
        return dll, plugin_path

    if sys.platform.startswith('win'):
        libname = 'libvlc.dll'
        p = find_library(libname)
        if p is None:
            try:  # some registry settings
                # leaner than win32api, win32con
                import winreg as w
                for r in w.HKEY_LOCAL_MACHINE, w.HKEY_CURRENT_USER:
                    try:
                        r = w.OpenKey(r, 'Software\\VideoLAN\\VLC')
                        plugin_path, _ = w.QueryValueEx(r, 'InstallDir')
                        w.CloseKey(r)
                        break
                    except w.error:
                        pass
            except ImportError:  # no PyWin32
                pass
            if plugin_path is None:
                # try some standard locations.
                programfiles = os.environ["ProgramFiles"]
                homedir = os.environ["HOMEDRIVE"]
                for p in ('{programfiles}\\VideoLan{libname}', '{homedir}:\\VideoLan{libname}',
                          '{programfiles}{libname}', '{homedir}:{libname}'):
                    p = p.format(homedir=homedir,
                                 programfiles=programfiles,
                                 libname='\\VLC\\' + libname)
                    if os.path.exists(p):
                        plugin_path = os.path.dirname(p)
                        break
            if plugin_path is not None:  # try loading
                # PyInstaller Windows fix
                if 'PyInstallerCDLL' in ctypes.CDLL.__name__:
                    ctypes.windll.kernel32.SetDllDirectoryW(None)
                p = os.getcwd()
                os.chdir(plugin_path)
                # if chdir failed, this will raise an exception
                dll = ctypes.CDLL('.\\' + libname)
                # restore cwd after dll has been loaded
                os.chdir(p)
            else:  # may fail
                dll = ctypes.CDLL('.\\' + libname)
        else:
            plugin_path = os.path.dirname(p)
            dll = ctypes.CDLL(p)

    elif sys.platform.startswith('darwin'):
        # FIXME: should find a means to configure path
        d = '/Applications/VLC.app/Contents/MacOS/'
        c = d + 'lib/libvlccore.dylib'
        p = d + 'lib/libvlc.dylib'
        if os.path.exists(p) and os.path.exists(c):
            # pre-load libvlccore VLC 2.2.8+
            ctypes.CDLL(c)
            dll = ctypes.CDLL(p)
            for p in ('modules', 'plugins'):
                p = d + p
                if os.path.isdir(p):
                    plugin_path = p
                    break
        else:  # hope, some [DY]LD_LIBRARY_PATH is set...
            # pre-load libvlccore VLC 2.2.8+
            ctypes.CDLL('libvlccore.dylib')
            dll = ctypes.CDLL('libvlc.dylib')

    else:
        # All other OSes (linux, freebsd...)
        p = find_library('vlc')
        try:
            dll = ctypes.CDLL(p)
        except OSError:  # may fail
            dll = None
        if dll is None:
            try:
                dll = ctypes.CDLL('libvlc.so.5')
            except:
                raise NotImplementedError('Cannot find libvlc lib')

    return dll


def set_proxy():
    http_proxy = os.environ.get('http_proxy') or os.environ.get('HTTP_PROXY')
    if http_proxy:
        return http_proxy
    if sys.platform != 'win32':
        return None
    try:
        import winreg
        # 打开 Windows 注册表
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            # 读取代理设置
            proxy_enable, _ = winreg.QueryValueEx(key, 'ProxyEnable')
            proxy_server, _ = winreg.QueryValueEx(key, 'ProxyServer')
            if proxy_enable == 1 and proxy_server:
                # 是否需要设置代理
                os.environ['http_proxy'] = 'http://%s' % proxy_server.replace("http://", '')
                os.environ['https_proxy'] = 'http://%s' % proxy_server.replace("http://", '')
                return proxy_server
    except Exception as e:
        print(f"Error accessing Windows registry: {e}")
    return None


# delete tmp files
def delete_temp(noextname=""):
    if noextname and os.path.exists(f"{config.rootdir}/tmp/{noextname}"):
        shutil.rmtree(f"{config.rootdir}/tmp/{noextname}")


#  get role by edge tts
def get_edge_rolelist():
    voice_list = {}
    if os.path.exists(config.rootdir + "/voice_list.json"):
        try:
            voice_list = json.load(open(config.rootdir + "/voice_list.json", "r", encoding="utf-8"))
            if len(voice_list) > 0:
                config.edgeTTS_rolelist = voice_list
                return voice_list
        except:
            pass
    v = asyncio.run(edge_tts.list_voices())
    for it in v:
        name = it['ShortName']
        prefix = name.split('-')[0].lower()
        if prefix not in voice_list:
            voice_list[prefix] = ["No", name]
        else:
            voice_list[prefix].append(name)
    json.dump(voice_list, open(config.rootdir + "/voice_list.json", "w"))
    config.edgeTTS_rolelist = voice_list
    return voice_list


# split audio by silence
def shorten_voice(normalized_sound):
    normalized_sound = match_target_amplitude(normalized_sound, -20.0)
    max_interval = 10000
    buffer = 500
    nonsilent_data = []
    audio_chunks = detect_nonsilent(normalized_sound, min_silence_len=int(config.video['voice_silence']),
                                    silence_thresh=-20 - 25)
    # print(audio_chunks)
    for i, chunk in enumerate(audio_chunks):
        start_time, end_time = chunk
        n = 0
        while end_time - start_time >= max_interval:
            n += 1
            # new_end = start_time + max_interval+buffer
            new_end = start_time + max_interval + buffer
            new_start = start_time
            nonsilent_data.append((new_start, new_end, True))
            start_time += max_interval
        nonsilent_data.append((start_time, end_time, False))
    return nonsilent_data


#
def match_target_amplitude(sound, target_dBFS):
    change_in_dBFS = target_dBFS - sound.dBFS
    return sound.apply_gain(change_in_dBFS)


# join all short audio to one ,eg name.mp4  name.mp4.wav
def merge_audio_segments(segments, start_times, total_duration, noextname):
    merged_audio = AudioSegment.empty()
    # start is not 0
    if start_times[0] != 0:
        silence_duration = start_times[0]
        silence = AudioSegment.silent(duration=silence_duration)
        merged_audio += silence

    # join
    for i in range(len(segments)):
        segment = segments[i]
        start_time = start_times[i]
        # add silence
        if i > 0:
            previous_end_time = start_times[i - 1] + len(segments[i - 1])
            silence_duration = start_time - previous_end_time
            # 前面一个和当前之间存在静音区间
            if silence_duration > 0:
                silence = AudioSegment.silent(duration=silence_duration)
                merged_audio += silence

        merged_audio += segment
    if total_duration > 0 and (len(merged_audio) < total_duration):
        # 末尾补静音
        silence = AudioSegment.silent(duration=total_duration - len(merged_audio))
        merged_audio += silence
    # 如果新长度大于原时长，则末尾截断
    if total_duration > 0 and (len(merged_audio) > total_duration):
        # 截断前先保存原完整文件
        merged_audio.export(f'{config.video["target_dir"]}/{noextname}/{config.video["target_language"]}-nocut.wav',format="wav")
        merged_audio = merged_audio[:total_duration]
    merged_audio.export(f"{config.rootdir}/tmp/{noextname}/tts-{noextname}.wav", format="wav")
    shutil.copy(
        f"{config.rootdir}/tmp/{noextname}/tts-{noextname}.wav",
                f"{config.video['target_dir']}/{noextname}/{config.video['target_language']}.wav"
    )
    return merged_audio


# speed change
def speed_change(sound, speed=1.0):
    # Manually override the frame_rate. This tells the computer how many
    # samples to play per second
    sound_with_altered_frame_rate = sound._spawn(sound.raw_data, overrides={
        "frame_rate": int(sound.frame_rate * speed)
    })
    # convert the sound with altered frame rate to a standard frame rate
    # so that regular playback programs will work right. They often only
    # know how to play audio at standard frame rate (like 44.1k)
    return sound_with_altered_frame_rate.set_frame_rate(sound.frame_rate)


def runffmpeg(arg, *, noextname=None):
    # 需要返回结果： 异步执行 ffmpeg，但同步阻塞等待，直到成功或失败返回
    cmd = "ffmpeg -hide_banner "
    if config.video['enable_cuda']:
        cmd += " -hwaccel cuda "
    if isinstance(arg, list):
        arg = " ".join(arg)
    cmd += arg
    logger.info(f"runffmpeg Will execute: {cmd=}")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    if noextname:
        config.queue_novice[noextname] = 'ing'

    while True:
        try:
            if config.ffmpeg_status == 'stop':
                set_process(f"ffmpeg停止了")
                p.terminate()
                p.kill()
                return
            rs = p.wait(1)
            if noextname:
                config.queue_novice[noextname] = "end" if rs == 0 else 'error'
            if rs != 0:
                set_process(f"[error]ffmpeg执行结果:失败 {cmd=},{p.stderr=}")
            return True
        except Exception as e:
            if config.ffmpeg_status == 'stop':
                p.terminate()
                p.kill()
                return
            print("ffmpeg 等待中:" + str(e))


# 文字合成
def text_to_speech(*, text="", role="", rate='+0%', filename=None, tts_type=None):
    try:
        if tts_type == "edgeTTS":
            if not get_voice_edgetts(text=text, role=role, rate=rate, filename=filename):
                logger.error(f"使用edgeTTS合成语音失败")
                open(filename, "w").close()
                return None
        elif tts_type == "openaiTTS":
            if not get_voice_openaitts(text, role, rate, filename):
                logger.error(f"使用openaiTTS合成语音失败")
                open(filename, "w").close()
                return False
        if os.path.exists(filename) and os.path.getsize(filename) > 0:
            return True
        return False
    except Exception as e:
        logger.error(f"文字合成出错:{filename=},{tts_type=}," + str(e))
        open(filename, "w").close()
        return False


def get_large_audio_transcriptioncli(noextname, mp4ext, showprocess):
    folder_path = config.rootdir + f'/tmp/{noextname}'
    aud_path = folder_path + f"/{noextname}.wav"
    sub_name = folder_path + f"/{noextname}.srt"
    mp4name = f"{noextname}{mp4ext}"
    showprocess(f"{mp4name} spilt audio", "logs")
    if config.current_status == 'stop':
        raise Exception("You stop it.")
    tmp_path = folder_path + f'/##{noextname}_tmp'
    if not os.path.isdir(tmp_path):
        os.makedirs(tmp_path, 0o777, exist_ok=True)
    r = sr.Recognizer()

    if not os.path.exists(sub_name) or os.path.getsize(sub_name) == 0:
        normalized_sound = AudioSegment.from_wav(aud_path)  # -20.0
        total_length = len(normalized_sound) / 1000
        nonslient_file = f'{tmp_path}/detected_voice.json'
        if os.path.exists(nonslient_file):
            with open(nonslient_file, 'r') as infile:
                nonsilent_data = json.load(infile)
        else:
            if config.current_status == 'stop':
                raise Exception("You stop it.")
            nonsilent_data = shorten_voice(normalized_sound)
            showprocess(f"{mp4name} split voice", 'logs')
            with open(nonslient_file, 'w') as outfile:
                json.dump(nonsilent_data, outfile)

        # subtitle
        subs = []
        # all audio chunk
        segments = []
        # every start time
        start_times = []

        # max words every line
        maxlen = 36 if config.video['target_language'][:2] in ["zh", "ja", "jp", "ko"] else 80
        for i, duration in enumerate(nonsilent_data):
            if config.current_status == 'stop':
                raise Exception("You stop it.")
            start_time, end_time, buffered = duration

            start_times.append(start_time)
            logger.info(f"{start_time=},{end_time=},{duration=}")
            time_covered = start_time / len(normalized_sound) * 100
            # 进度
            showprocess(f"{mp4name} {time_covered:.1f}%", 'logs')
            chunk_filename = tmp_path + f"/c{i}_{start_time // 1000}_{end_time // 1000}.wav"
            add_vol = 0
            audio_chunk = normalized_sound[start_time:end_time] + add_vol
            audio_chunk.export(chunk_filename, format="wav")

            # recognize the chunk
            with sr.AudioFile(chunk_filename) as source:
                audio_listened = r.record(source)
                logger.info(f"sr.AudioFile:{chunk_filename=}")
                try:
                    options = {"download_root": config.rootdir + "/models"}
                    text = r.recognize_whisper(audio_listened,
                                               language="zh" if config.video['detect_language'] == "zh-cn" or
                                                                config.video['detect_language'] == "zh-tw" else
                                               config.video['detect_language'],
                                               model=config.video['whisper_model'],
                                               load_options=options)
                except sr.UnknownValueError as e:
                    logger.error("Recognize Error: ", str(e))
                    segments.append(audio_chunk)
                    continue
                except Exception as e:
                    logger.error("Recognize Error:", str(e))
                    segments.append(audio_chunk)
                    continue
                if config.current_status == 'stop':
                    raise Exception("You stop it.")
                text = f"{text.capitalize()}. "
                try:
                    print(f"translate_type============={config.video['translate_type']}")
                    # if config.video['translate_type'] == 'google':
                    #     result = googletrans(text, config.video['source_language'],
                    #                          config.video['target_language'])
                    #     print(f"{result=}")
                    # elif config.video['translate_type'] == 'baidu':
                    #     result = baidutrans(text, 'auto', config.video['target_language'])
                    # elif config.video['translate_type'] == 'tencent':
                    #     result = baidutrans(text, 'auto', config.video['target_language'])
                    # elif config.video['translate_type'] == 'chatGPT':
                    #     result = chatgpttrans(text)

                    if config.video['translate_type'] == 'baidu':
                        result = baidutrans(text, 'auto', config.video['target_language_baidu'])
                    elif config.video['translate_type'] == 'tencent':
                        result = tencenttrans(text, 'auto', config.video['target_language_tencent'])
                    elif config.video['translate_type'] == 'baidu(noKey)':
                        result = baidutrans_spider.baidutrans(text, 'auto',
                                                              config.video['target_language_baidu'])
                    elif config.video['translate_type'] == 'DeepL':
                        result = deepltrans(text, config.video['target_language_deepl'])
                    elif config.video['translate_type'] == 'DeepLX':
                        result = deeplxtrans(text, config.video['target_language_deepl'])
                    else:
                        result = googletrans(text, config.video['source_language'],
                                             config.video['target_language'])
                    logger.info(f"target_language={config.video['target_language']},[translate ok]\n")
                except Exception as e:
                    logger.error("Translate Error:", str(e))
                    segments.append(audio_chunk)
                    continue
                # exists text vaild
                isemtpy = True
                if not re.fullmatch(r'^[./\\。，/\s]*$', result.strip(), re.I):
                    isemtpy = False
                    combo_txt = result + '\n\n'
                    if len(result) > maxlen:
                        if maxlen == 36:
                            # zh ja ko
                            result_tmp = ""
                            for tmp_i in range(1 + len(result) // maxlen):
                                result_tmp += result[tmp_i * maxlen:tmp_i * maxlen + maxlen] + "\n"
                            combo_txt = result_tmp.strip() + '\n\n'
                        else:
                            # en
                            combo_txt = textwrap.fill(result, maxlen) + "\n\n"
                    if buffered:
                        end_time -= 500
                    start = timedelta(milliseconds=start_time)
                    end = timedelta(milliseconds=end_time)

                    index = len(subs) + 1

                    sub = srt.Subtitle(index=index, start=start, end=end, content=combo_txt)
                    showprocess(f"{start} --> {end} {combo_txt}", 'subtitle')
                    subs.append(sub)

                #  voice role
                if config.video['voice_role'] != 'No':
                    if isemtpy:
                        segments.append(AudioSegment.silent(duration=end_time - start_time))
                        continue
                    try:
                        rate = int(str(config.video['voice_rate']).replace('%', ''))
                        if rate >= 0:
                            rate = f"+{rate}%"
                        else:
                            rate = f"{rate}%"
                        tmpname = f"{folder_path}/tts-{start_time}-{index}.mp3"
                        tts_result = text_to_speech(
                            text=result,
                            role=config.video['voice_role'],
                            rate=rate,
                            filename=tmpname,
                            tts_type=config.video['tts_type'])
                        if not tts_result:
                            showprocess(f"tts合成出错:{result=}", 'logs')
                            segments.append(audio_chunk)
                            continue

                        audio_data = AudioSegment.from_file(tmpname, format="mp3")
                        wavlen = end_time - start_time
                        mp3len = len(audio_data)
                        if config.video['voice_autorate'] and (mp3len - wavlen > 1000):
                            # 最大加速2倍
                            speed = mp3len / wavlen
                            speed = 2 if speed > 2 else speed
                            showprocess(f"new mp3 length bigger than wav ,speed up {speed} ", 'logs')
                            audio_data = speed_change(audio_data, speed)
                            showprocess(f"change after:{len(audio_data)}", 'logs')

                        segments.append(audio_data)
                    except Exception as e:
                        logger.error("Create voice role error:" + str(e))
                        print(e)
                        segments.append(audio_chunk)
        # merge translate audo
        merge_audio_segments(segments, start_times, total_length * 1000, noextname)
        final_srt = srt.compose(subs)
        with open(sub_name, 'w', encoding="utf-8") as f:
            f.write(final_srt)
    showprocess(f"{mp4name} add subtitle", 'logs')
    compos_video(config.video['source_mp4'], noextname)


def show_popup(title, text):
    msg = QMessageBox()
    msg.setWindowTitle(title)
    msg.setWindowIcon(QIcon(config.rootdir + "/icon.ico"))
    msg.setText(text)
    msg.addButton(transobj['queding'], QMessageBox.AcceptRole)
    msg.addButton("Cancel", QMessageBox.RejectRole)
    msg.setIcon(QMessageBox.Information)
    # msg.setStandardButtons(QMessageBox.Ok)
    x = msg.exec_()  # 显示消息框

    return x


'''
print(ms_to_time_string(ms=12030))
-> 00:00:12,030
'''


def ms_to_time_string(*,ms=0,seconds=None):
    # 计算小时、分钟、秒和毫秒
    if seconds is None:
        td = timedelta(milliseconds=ms)
    else:
        td = timedelta(seconds=seconds)
    hours, remainder = divmod(td.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    milliseconds = td.microseconds // 1000

    # 格式化为字符串
    time_string = f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

    return time_string


# noextname 是去掉 后缀mp4的视频文件名字
# 所有临时文件保存在 /tmp/noextname文件夹下
# 分批次读取
def recognition_translation_split(noextname):
    set_process("准备分割数据后进行语音识别")
    folder_path = config.rootdir + f'/tmp/{noextname}'
    aud_path = folder_path + f"/{noextname}.wav"
    sub_name = folder_path + f"/{noextname}.srt"
    if config.current_status == 'stop':
        raise Exception("You stop it.")
    # create
    # temp dir
    tmp_path = folder_path + f'/##{noextname}_tmp'
    if not os.path.isdir(tmp_path):
        try:
            os.makedirs(tmp_path, 0o777, exist_ok=True)
        except:
            show_popup(transobj["anerror"], transobj["createdirerror"])

    # 已存在字幕文件
    if os.path.exists(sub_name) and os.path.getsize(sub_name) > 0:
        set_process(f"{noextname} 字幕文件已存在，直接使用", 'logs')
        return

    normalized_sound = AudioSegment.from_wav(aud_path)  # -20.0
    nonslient_file = f'{tmp_path}/detected_voice.json'
    if os.path.exists(nonslient_file) and os.path.getsize(nonslient_file):
        with open(nonslient_file, 'r') as infile:
            nonsilent_data = json.load(infile)
    else:
        if config.current_status == 'stop':
            raise Exception("You stop it.")
        nonsilent_data = shorten_voice(normalized_sound)
        set_process(f"{noextname} 对音频文件按静音片段分割处理", 'logs')
        with open(nonslient_file, 'w') as outfile:
            json.dump(nonsilent_data, outfile)

    # subtitle
    subs = []
    r = sr.Recognizer()
    logger.info("for i in nonsilent_data")
    raw_subtitles = ""
    offset=0
    for i, duration in enumerate(nonsilent_data):
        if config.current_status == 'stop':
            raise Exception("You stop it.")
        start_time, end_time, buffered = duration
        start_time+=offset
        end_time+=offset
        if start_time == end_time:
            end_time+=200
            # 如果加了200后，和下一个开始重合，则偏移
            if (i < len(nonsilent_data)-1) and nonsilent_data[i+1][0]<end_time:
                offset+=200
        time_covered = start_time / len(normalized_sound) * 100
        # 进度
        set_process(f"{noextname} 音频处理进度{time_covered:.1f}%", 'logs')
        chunk_filename = tmp_path + f"/c{i}_{start_time // 1000}_{end_time // 1000}.wav"
        add_vol = 0
        audio_chunk = normalized_sound[start_time:end_time] + add_vol
        audio_chunk.export(chunk_filename, format="wav")

        # recognize the chunk
        with sr.AudioFile(chunk_filename) as source:
            audio_listened = r.record(source)
            logger.info(f"sr.AudioFile:{chunk_filename=}")
            if config.current_status == 'stop':
                raise Exception("You stop it.")
            try:
                options = {"download_root": config.rootdir + "/models"}
                text = r.recognize_whisper(audio_listened,
                                           language="zh" if config.video['detect_language'] == "zh-cn" or
                                                            config.video['detect_language'] == "zh-tw" else
                                           config.video['detect_language'],
                                           model=config.video['whisper_model'],
                                           load_options=options)
            except sr.UnknownValueError as e:
                set_process("[error]:语音识别出错了:" + str(e))
                continue
            except Exception as e:
                set_process("[error]:语音识别出错了:" + str(e))
                continue
            if config.current_status == 'stop':
                raise Exception("You stop it.")
            text = f"{text.capitalize()}. ".replace('&#39;',"'")
            text=re.sub(r'&#\d+;','',text)
            # 翻译
            try:
                index = len(subs) + 1
                if buffered:
                    end_time -= 500
                start = timedelta(milliseconds=start_time)
                end = timedelta(milliseconds=end_time)
                raw_subtitles += f"{index}\n{start} --> {end}\n{text}\n\n"
                if config.video['translate_type'] == 'google':
                    result = googletrans(text, config.video['source_language'],
                                         config.video['target_language'])
                elif config.video['translate_type'] == 'baidu':
                    result = baidutrans(text, 'auto', config.video['target_language_baidu'])
                elif config.video['translate_type'] == 'tencent':
                    result = tencenttrans(text, 'auto', config.video['target_language_tencent'])
                elif config.video['translate_type'] == 'baidu(noKey)':
                    result = baidutrans_spider.baidutrans(text, 'auto', config.video['target_language_baidu'])
                elif config.video['translate_type'] == 'DeepL':
                    result = deepltrans(text, config.video['target_language_deepl'])
                elif config.video['translate_type'] == 'DeepLX':
                    result = deeplxtrans(text, config.video['target_language_deepl'])
                elif config.video['translate_type'] == 'chatGPT':
                    result = chatgpttrans(text)
                    logger.info(f"target_language={config.video['target_language']},[translate ok]\n")
                    sub = srt.Subtitle(index=index, start=start, end=end, content=text)
                    subs.append(sub)
                    continue

                logger.info(f"target_language={config.video['target_language']},[translate ok]\n")
            except Exception as e:
                logger.error("Translate Error:", str(e))
                continue

            combo_txt = result.strip().replace('&#39;',"'") + "\n\n"
            combo_txt=re.sub(r'&#\d+;','',combo_txt)
            sub = srt.Subtitle(index=index, start=start, end=end, content=combo_txt)
            subs.append(sub)
            set_process(
                srt.compose([srt.Subtitle(index=index, start=start, end=end, content=combo_txt)], reindex=False),
                'subtitle')
    save_raw_subtitle(raw_subtitles, noextname, config.video['source_language'])
    final_srt = srt.compose(subs)
    if config.video['translate_type'] == 'chatGPT':
        set_process(f"{noextname} 等待 chatGPT 返回响应", 'logs')
        final_srt = chatgpttrans(final_srt)
        if final_srt.startswith('[error]'):
            config.current_status = "stop"
            config.subtitle_end = False
            set_process(f"[error]:{noextname} ChatGPT 翻译出错:{final_srt}", 'logs')
            return
        set_process(f"{noextname} chatGPT OK", 'logs')

    #    对字幕进行单行截断操作
    if not final_srt.strip():
        set_process(f"[error]{noextname} 字幕创建失败", 'logs')
        config.current_status = "stop"
        config.subtitle_end = False
        return
    # 保存字幕到临时tmp
    with open(sub_name, 'w', encoding="utf-8") as f:
        final_srt = final_srt.replace('&#39;', "'")
        final_srt = re.sub(r'&#\d+;', '', final_srt)
        f.write(final_srt.strip())
        set_process(final_srt.strip(), 'replace_subtitle')

    set_process(f"{noextname} 字幕处理完成，等待修改", 'logs')


def recognition_translation_all(noextname):
    folder_path = config.rootdir + f'/tmp/{noextname}'
    audio_path = folder_path + f"/{noextname}.wav"
    sub_name = folder_path + f"/{noextname}.srt"
    model = config.video['whisper_model']
    language = config.video['detect_language']
    set_process(f"准备进行整体语音识别,可能耗时较久，请等待:{model}模型")
    try:
        model = whisper.load_model(model, download_root=config.rootdir + "/models")  # Change this to your desired model
        transcribe = model.transcribe(audio_path, language="zh" if language in ["zh-cn", "zh-tw"] else language, )
        segments = transcribe['segments']
        subtitles = ""
        # 保留原始语言的字幕
        raw_subtitles = ""
        line_num = 0
        offset=0
        for (sidx,segment) in enumerate(segments):
            if config.current_status == 'stop' or config.current_status == 'end':
                return
            segment['start']=int(segment['start']*1000)+offset
            segment['end']=int(segment['end']*1000)+offset
            if segment['start']==segment['end']:
                segment['end']+=200
                if sidx < len(segments)-1 and (int(segments[sidx+1]['start']*1000)<segment['end']):
                    offset+=200
            startTime = ms_to_time_string(ms=segment['start'])
            endTime = ms_to_time_string(ms=segment['end'])
            text = segment['text'].strip().replace('&#39;',"'")
            text=re.sub(r'&#\d+;','',text)
            set_process(f"识别到字幕：{startTime} --> {endTime}")
            # 无有效字符
            if not text or re.match(r'^[，。、？‘’“”；：（｛｝【】）:;"\'\s \d`!@#$%^&*()_+=.,?/\\-]*$', text) or len(text) <= 1:
                continue
            line_num += 1
            # 原语言字幕
            raw_subtitles += f"{line_num}\n{startTime} --> {endTime}\n{text}\n\n"
            if config.video['translate_type'] == 'chatGPT':
                # 如果是 chatGPT，直接组装字幕
                continue
            # 开始翻译
            new_text = text
            if config.video['translate_type'] == 'google':
                new_text = googletrans(text, config.video['source_language'],
                                       config.video['target_language'])

            elif config.video['translate_type'] == 'baidu':
                new_text = baidutrans(text, 'auto', config.video['target_language_baidu'])
            elif config.video['translate_type'] == 'tencent':
                new_text = tencenttrans(text, 'auto', config.video['target_language_tencent'])
            elif config.video['translate_type'] == 'baidu(noKey)':
                new_text = baidutrans_spider.baidutrans(text, 'auto', config.video['target_language_baidu'])
            elif config.video['translate_type'] == 'DeepL':
                new_text = deepltrans(text, config.video['target_language_deepl'])
            elif config.video['translate_type'] == 'DeepLX':
                new_text = deeplxtrans(text, config.video['target_language_deepl'])
            new_text = new_text.replace('&#39;', "'")
            new_text = re.sub(r'&#\d+;', '', new_text)
            current_sub = f"{line_num}\n{startTime} --> {endTime}\n{new_text}\n\n"
            subtitles += current_sub
            set_process(current_sub, 'subtitle')

        # 写入原语言字幕
        save_raw_subtitle(raw_subtitles, noextname, config.video['source_language'])
        if config.video['translate_type'] == 'chatGPT':
            set_process(f"等待 chatGPT 返回响应", 'logs')
            subtitles = chatgpttrans(raw_subtitles)
            if subtitles.startswith('[error]'):
                config.current_status = "stop"
                config.subtitle_end = False
                set_process(f"[error]:ChatGPT 翻译出错:{subtitles}", 'logs')
                return
            set_process(f"chatGPT OK", 'logs')
        # 保存字幕到tmp待处理
        with open(sub_name, 'w', encoding="utf-8") as f:
            subtitles = subtitles.replace('&#39;', "'")
            subtitles = re.sub(r'&#\d+;', '', subtitles)
            f.write(subtitles.strip())
            set_process(subtitles.strip(), 'replace_subtitle')
        set_process(f"{noextname} 字幕处理完成，等待修改", 'logs')
        return True
    except Exception as e:
        set_process(f"{model}模型整体识别出错了:{str(e)}")
        config.current_status = "stop"
        config.subtitle_end = False
        return


# 保存字幕文件 到目标文件夹
def save_raw_subtitle(srtstr, noextname, language):
    file = f"{config.video['target_dir']}/{noextname}/{language}.srt"
    if not os.path.exists(os.path.dirname(file)):
        os.makedirs(os.path.dirname(file), exist_ok=True)
    with open(file, 'w', encoding="utf-8") as f:
        f.write(srtstr.strip())


# 从字幕文件获取格式化后的字幕信息
'''
[
{'line': 13, 'time': '00:01:56,423 --> 00:02:06,423', 'text': '因此，如果您准备好停止沉迷于不太理想的解决方案并开始构建下一个
出色的语音产品，我们已准备好帮助您实现这一目标。深度图。没有妥协。唯一的机会..', 'startraw': '00:01:56,423', 'endraw': '00:02:06,423', 'start_time'
: 116423, 'end_time': 126423}, 
{'line': 14, 'time': '00:02:06,423 --> 00:02:07,429', 'text': '机会..', 'startraw': '00:02:06,423', 'endraw': '00:02
:07,429', 'start_time': 126423, 'end_time': 127429}
]
'''
def get_subtitle_from_srt(srtfile,*,is_file=True):
    if is_file:
        with open(srtfile, 'r', encoding="utf-8") as f:
            txt = f.read().strip().split("\n")
    else:
        txt=srtfile.strip()
    # 行号
    line = 0
    maxline = len(txt)
    # 行格式
    linepat = r'^\s*?\d+\s*?$'
    # 时间格式
    timepat = r'^\s*?\d+:\d+:\d+\,?\d*?\s*?-->\s*?\d+:\d+:\d+\,?\d*?$'
    result = []
    for i, t in enumerate(txt):
        # 当前行 小于等于倒数第三行 并且匹配行号，并且下一行匹配时间戳，则是行号
        if i < maxline - 2 and re.match(linepat, t) and re.match(timepat, txt[i + 1]):
            #   是行
            line += 1
            obj = {"line": line, "time": "", "text": ""}
            result.append(obj)
        elif re.match(timepat, t):
            # 是时间行
            result[line - 1]['time'] = t
        elif len(t.strip()) > 0:
            # 是内容
            txt_tmp=t.strip().replace('&#39;',"'")
            txt_tmp=re.sub(r'&#\d+;','',txt_tmp)
            result[line - 1]['text'] += txt_tmp
    # 再次遍历，删掉美元text的行
    new_result = []
    line = 1
    for it in result:
        if "text" in it and len(it['text'].strip()) > 0 and not re.match(r'^[,./?`!@#$%^&*()_+=\\|\[\]{}~\s \n-]*$',it['text']):
            it['line'] = line
            startraw, endraw = it['time'].strip().split(" --> ")
            start = startraw.replace(',', '.').split(":")
            start_time = int(int(start[0]) * 3600000 + int(start[1]) * 60000 + float(start[2]) * 1000)
            end = endraw.replace(',', '.').split(":")
            end_time = int(int(end[0]) * 3600000 + int(end[1]) * 60000 + float(end[2]) * 1000)
            it['startraw'] = startraw
            it['endraw'] = endraw
            it['start_time'] = start_time
            it['end_time'] = end_time
            new_result.append(it)
            line += 1
    return new_result


# 合并
# source_mp4 原始MP4地址，具体到 后缀
# noextname，无后缀的mp4文件名字
# mp4ext .mp4后缀，可能存在大小写问题
# 配音预处理，去掉无效字符，整理开始时间
def dubbing(noextname, only_dubbing=False):
    # 所有临时文件均产生在 tmp/无后缀mp4名文件夹
    folder_path = config.rootdir + f'/tmp/{noextname}'
    # 如果仅仅生成配音，则不限制时长
    if only_dubbing:
        total_length = 0
    else:
        normalized_sound = AudioSegment.from_wav(f"{folder_path}/{noextname}.wav")
        total_length = len(normalized_sound) / 1000
    sub_name = f"{folder_path}/{noextname}.srt"
    tts_wav = f"{folder_path}/tts-{noextname}.wav"
    logger.info(f"准备合成语音 {folder_path=}")
    # 整合一个队列到 exec_tts 执行
    queue_tts = []
    if (config.video['voice_role'] != 'No') and (not os.path.exists(tts_wav) or os.path.getsize(tts_wav) == 0):
        # 获取字幕
        subs = get_subtitle_from_srt(sub_name)
        logger.info(f"Creating TTS wav {tts_wav}")
        rate = int(str(config.video['voice_rate']).replace('%', ''))
        if rate >= 0:
            rate = f"+{rate}%"
        else:
            rate = f"{rate}%"
        # 取出每一条字幕，行号\n开始时间 --> 结束时间\n内容
        for it in subs:
            if config.current_status == 'stop' or config.current_status == 'end':
                raise Exception("You stop it.")
            queue_tts.append({
                "text": it['text'],
                "role": config.video['voice_role'],
                "start_time": it['start_time'],
                "end_time": it['end_time'],
                "rate": rate,
                "startraw": it['startraw'],
                "endraw": it['endraw'],
                "filename": f"{folder_path}/tts-{it['start_time']}.mp3"})
        exec_tts(queue_tts, total_length, noextname)

# 获取某个视频的时长
def get_video_duration(file_path):
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path
    ]

    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        duration = int(float(result.stdout.strip()) * 1000)
        return duration
    except subprocess.CalledProcessError as e:
        print(f"Error: {e}")
        return None

# 判断 novoice.mp4是否创建好
def is_novoice_mp4(novoice_mp4,noextname):
    # 预先创建好的
    # 判断novoice_mp4是否完成
    t=0
    while True:
        if config.current_status != 'ing':
            return False
        if t>18 and os.path.exists(novoice_mp4) and os.path.getsize(novoice_mp4)>0:
            return True
        if noextname not in config.queue_novice:
            msg = f"抱歉，视频{noextname} 预处理 novoice 失败,请重试"
            set_process(msg)
            return False
        if config.queue_novice[noextname] == 'error':
            msg = f"抱歉，视频{noextname} 预处理 novoice 失败"
            set_process(msg)
            return False

        if config.queue_novice[noextname] == 'ing':
            set_process(f"{noextname} 所需资源未准备完毕，请稍等..{config.queue_novice[noextname]=}")
            time.sleep(3)
            t+=3
            continue
        return True

# 视频自动降速播放
def video_autorate_process(noextname, queue_params, source_mp4_total_length):
    segments = []
    start_times = []
    folder_path = config.rootdir + f'/tmp/{noextname}'
    # 已存在的原始 novoice_mp4
    novoice_mp4 = f"{folder_path}/novoice.mp4"
    # 处理过程中不断变化的 novoice_mp4
    novoice_mp4_tmp = f"{folder_path}/novoice_tmp.mp4"
    queue_copy = copy.deepcopy(queue_params)
    # 预先创建好的
    novoice_mp4 = f"{folder_path}/novoice.mp4"
    # 判断novoice_mp4是否完成
    if not is_novoice_mp4(novoice_mp4,noextname):
        return
    try:
        # 增加的时间，用于 修改字幕里的开始显示时间和结束时间
        offset = 0
        total_length = 0
        last_index = len(queue_params) - 1
        set_process(f"原mp4长度={source_mp4_total_length=}")
        line_num=0
        cut_clip=0
        for (idx, it) in enumerate(queue_params):
            if config.current_status != 'ing':
                return
            # 原发音时间段长度
            wavlen = it['end_time'] - it['start_time']
            if wavlen == 0:
                # 舍弃
                continue
            line_num+=1
            set_process(f"<br>[{line_num=}]<br>before: {it['startraw']=},{it['endraw']=}")
            # 该片段配音失败
            if not os.path.exists(it['filename']) or os.path.getsize(it['filename']) == 0:
                total_length += wavlen
                it['start_time'] += offset
                it['end_time'] = it['start_time'] + wavlen
                it['startraw'] = ms_to_time_string(ms=it['start_time'])
                it['endraw'] = ms_to_time_string(ms=it['end_time'])
                start_times.append(it['start_time'])
                segments.append(AudioSegment.silent(duration=wavlen))
                queue_params[idx] = it
                set_process(f"[error]: 此 {it['startraw']} - {it['endraw']} 时间段内字幕合成语音失败", 'logs')
                continue
            audio_data = AudioSegment.from_file(it['filename'], format="mp3")

            # 新发音长度
            mp3len = len(audio_data)
            # 先判断，如果 新时长大于旧时长，需要处理，这个最好需要加到 offset
            diff = mp3len - wavlen
            # 新时长大于旧时长，视频需要降速播放
            if diff > 0:
                # 总时长 毫秒
                total_length += mp3len
                # 调整视频，新时长/旧时长
                pts = round(mp3len / wavlen, 2)
                # 第一个命令
                startmp4 = f"{folder_path}/novice-{idx}-start.mp4"
                clipmp4 = f"{folder_path}/novice-{idx}-clip.mp4"
                endmp4 = f"{folder_path}/novice-{idx}-end.mp4"
                # 开始时间要加上 offset
                it['start_time'] += offset
                it['end_time'] = it['start_time'] + mp3len
                it['startraw'] = ms_to_time_string(ms=it['start_time'])
                it['endraw'] = ms_to_time_string(ms=it['end_time'])

                set_process(f"after : {it['startraw']=},{it['endraw']=}")
                set_process(f"{diff=},{offset=},{wavlen=},{mp3len=},{pts=}")
                set_process(f"{startmp4=}<br>{clipmp4=}<br>{endmp4=}\n")

                offset += diff


                if cut_clip == 0 and it['start_time'] == 0:
                    set_process(f"当前是第一个，并且以0时间值开始，需要 clipmp4和endmp4 2个片段")
                    # 当前是第一个并且从头开始，不需要 startmp4, 共2个片段直接截取 clip 和 end
                    runffmpeg([
                        "-y",
                        "-ss",
                        '0',
                        "-t",
                        queue_copy[idx]['endraw'].replace(',', '.'),  # 持续时间
                        "-i",
                        f'"{novoice_mp4}"',
                        "-vf",
                        f'"setpts={pts}*PTS"',
                        "-c:v",
                        "libx264",
                        f'"{clipmp4}"'
                    ])
                    runffmpeg([
                        "-y",
                        "-ss",
                        queue_copy[idx]['endraw'].replace(',', '.'),
                        "-i",
                        f'"{novoice_mp4}"',
                        "-c",
                        "copy",
                        f'"{endmp4}"'
                    ])
                elif cut_clip == 0 and it['start_time'] > 0:
                    set_process(f"当前是第一个，但不是以0时间值开始，需要 startmp4 clipmp4和endmp4 3个片段")
                    # 如果是第一个，并且不是从头开始的，则从原始提取开头的片段，startmp4 climp4 endmp4
                    runffmpeg([
                        "-y",
                        "-ss",
                        "0",
                        "-t",
                        queue_copy[idx]["startraw"].replace(',', '.'),
                        "-i",
                        f'"{novoice_mp4}"',
                        "-c",
                        "copy",
                        f'"{startmp4}"'
                    ])
                    # 从原始提取当前切片
                    runffmpeg([
                        "-y",
                        "-ss",
                        queue_copy[idx]['startraw'].replace(',', '.'),
                        "-to",
                        queue_copy[idx]['endraw'].replace(',', '.'),  # 如果开始结束时间相同，则强制持续时间1s
                        "-i",
                        f'"{novoice_mp4}"',
                        "-vf",
                        f'"setpts={pts}*PTS"',
                        "-c:v",
                        "libx264",
                        f'"{clipmp4}"'
                    ])
                    # 从原始提取结束 end
                    runffmpeg([
                        "-y",
                        "-ss",
                        queue_copy[idx]['endraw'].replace(',', '.'),
                        "-i",
                        f'"{novoice_mp4}"',
                        "-c",
                        "copy",
                        f'"{endmp4}"'
                    ])
                elif (idx == last_index) and queue_copy[idx]['end_time'] < source_mp4_total_length:
                    #  是最后一个，但没到末尾，后边还有片段
                    #  开始部分从 todo 开始需要从 tmp 里获取
                    set_process(f"当前是最后一个，没到末尾，需要 startmp4和 clipmp4")
                    runffmpeg([
                        "-y",
                        "-ss",
                        "0",
                        "-t",
                        it["startraw"].replace(',', '.'),
                        "-i",
                        f'"{novoice_mp4_tmp}"' if os.path.exists(novoice_mp4_tmp) else f'"{novoice_mp4}"',
                        "-c",
                        "copy",
                        f'"{startmp4}"'
                    ])
                    # 当前需要从 原始获取
                    runffmpeg([
                        "-y",
                        "-ss",
                        queue_copy[idx]['startraw'].replace(',', '.'),
                        "-to",
                        queue_copy[idx]['endraw'].replace(',', '.'),  # 如果开始结束时间相同，则强制持续时间1s
                        "-i",
                        f'"{novoice_mp4}"',
                        "-vf",
                        f'"setpts={pts}*PTS"',
                        "-c:v",
                        "libx264",
                        f'"{clipmp4}"'
                    ])
                    # 从原始获取末尾，如果当前是最后一个，并且原始里没有结束 从原始里 截取开始时间
                    if queue_copy[idx]['start_time'] + mp3len < source_mp4_total_length:
                        set_process(f"还需要endmp4")
                        runffmpeg([
                            "-y",
                            "-ss",
                            queue_copy[idx]['endraw'].replace(',', '.'),
                            "-i",
                            f'"{novoice_mp4}"',
                            "-c",
                            "copy",
                            f'"{endmp4}"'
                        ])
                    else:
                        set_process(f"不需要endmp4")
                elif (idx == last_index) and queue_copy[idx]['end_time'] >= source_mp4_total_length:
                    # 是 最后一个，并且后边没有了,只有 startmp4 和 clip
                    set_process(f"当前是最后一个，并且到达结尾，只需要 startmp4和 clipmp4 2个片段")
                    # todo 需要从 tmp获取
                    runffmpeg([
                        "-y",
                        "-ss",
                        "0",
                        "-t",
                        it["startraw"].replace(',', '.'),
                        "-i",
                        f'"{novoice_mp4_tmp}"' if os.path.exists(novoice_mp4_tmp) else f'"{novoice_mp4}"',
                        "-c",
                        "copy",
                        f'"{startmp4}"'
                    ])
                    # 从原始获取当前切片直到结束
                    runffmpeg([
                        "-y",
                        "-ss",
                        queue_copy[idx]['startraw'].replace(',', '.'),
                        "-i",
                        f'"{novoice_mp4}"',
                        "-vf",
                        f'"setpts={pts}*PTS"',
                        "-c:v",
                        "libx264",
                        f'"{clipmp4}"'
                    ])
                elif cut_clip>0:
                    # 处于中间的其他情况，有前后中 3个
                    # start todo 需要从 tmp 获取
                    set_process(f"当前是第{idx + 1}个，需要 startmp4和 clipmp4和endmp4 3个片段")
                    runffmpeg([
                        "-y",
                        "-ss",
                        "0",
                        "-t",
                        it["startraw"].replace(',', '.'),
                        "-i",
                        f'"{novoice_mp4_tmp}"' if os.path.exists(novoice_mp4_tmp) else f'"{novoice_mp4}"',
                        "-c",
                        "copy",
                        f'"{startmp4}"'
                    ])
                    # 从原始获取切片
                    runffmpeg([
                        "-y",
                        "-ss",
                        queue_copy[idx]['startraw'].replace(',', '.'),
                        "-to",
                        queue_copy[idx]['endraw'].replace(',', '.'),
                        "-i",
                        f'"{novoice_mp4}"',
                        "-vf",
                        f'"setpts={pts}*PTS"',
                        "-c:v",
                        "libx264",
                        f'"{clipmp4}"'
                    ])
                    # 从原始获取结束
                    runffmpeg([
                        "-y",
                        "-ss",
                        queue_copy[idx]['endraw'].replace(',', '.'),
                        "-i",
                        f'"{novoice_mp4}"',
                        "-c",
                        "copy",
                        f'"{endmp4}"'
                    ])

                # 合并这个3个
                # ffmpeg.exe -i pre.mp4 -i clip.mp4 -i post.mp4 -filter_complex "[0:v][1:v][2:v]concat=n=3:v=1:a=0[outv]" -map "[outv]" -c:v libx264 -preset veryfast -an -y output.mp4
                if os.path.exists(startmp4) and os.path.exists(endmp4) and os.path.exists(clipmp4):
                    runffmpeg(
                        f'-y -i "{startmp4}" -i "{clipmp4}" -i "{endmp4}" -filter_complex "[0:v][1:v][2:v]concat=n=3:v=1:a=0[outv]" -map "[outv]" -c:v libx264  -an "{novoice_mp4_tmp}"')
                    set_process(f"3个合并")
                elif os.path.exists(startmp4) and os.path.exists(clipmp4):
                    runffmpeg(
                        f'-y -i "{startmp4}" -i "{clipmp4}"   -filter_complex "[0:v][1:v]concat=n=2:v=1:a=0[outv]" -map "[outv]" -c:v libx264  -an "{novoice_mp4_tmp}"')
                    set_process(f"startmp4 和 clipmp4 合并")
                elif os.path.exists(endmp4) and os.path.exists(clipmp4):
                    runffmpeg(
                        f'-y -i "{clipmp4}" -i "{endmp4}"  -filter_complex "[0:v][1:v]concat=n=2:v=1:a=0[outv]" -map "[outv]" -c:v libx264 -an "{novoice_mp4_tmp}"')
                    set_process(f"endmp4 和 clipmp4 合并")
                cut_clip+=1
                queue_params[idx] = it
            else:
                set_process(f"无需降速 {diff=}")
                total_length += wavlen
                it['start_time'] += offset
                it['end_time'] = it['start_time'] + wavlen
                it['startraw'] = ms_to_time_string(ms=it['start_time'])
                it['endraw'] = ms_to_time_string(ms=it['end_time'])
                queue_params[idx] = it
            start_times.append(it['start_time'])
            segments.append(audio_data)
            set_process(f"[{line_num}] 结束了<br>====mp3.length={total_length=}=====<br>")
        set_process(f"<br>原长度:{source_mp4_total_length=}, +offset={source_mp4_total_length + offset}")
        total_length = source_mp4_total_length + offset
        if os.path.exists(novoice_mp4_tmp):
            os.rename(novoice_mp4, folder_path+f"/novice.mp4.raw.mp4")
            os.rename(novoice_mp4_tmp, novoice_mp4)
            total_length = get_video_duration(novoice_mp4)
        if total_length is None:
            total_length = source_mp4_total_length + offset
        set_process(f"新视频长度:{total_length=}")
        # 重新修改字幕
        srt = ""
        for (idx, it) in enumerate(queue_params):
            srt += f"{idx + 1}\n{it['startraw']} --> {it['endraw']}\n{it['text']}\n\n"
        srt = srt.strip()
        # 修改tmp临时字幕
        with open(f"{folder_path}/{noextname}.srt", 'w', encoding='utf-8') as f:
            f.write(srt)
        merge_audio_segments(segments, start_times, total_length, noextname)
    except Exception as e:
        set_process("[error]视频自动降速处理出错了" + str(e))
        config.current_status='stop'

# 复制 novoice_mp4最后一帧，直到
def add_clip_to_last(noextname,duration_ms):
    folder_path = config.rootdir + f'/tmp/{noextname}'
    novoice_mp4 = f"{folder_path}/novoice.mp4"
    # 提取 1.mp4 的最后一帧为 1.png
    output_image = f"{folder_path}/{time.time()}.png"

    cap = cv2.VideoCapture(novoice_mp4)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    # 设置最后一帧为输出帧
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_count - 1)
    ret, frame = cap.read()
    if ret:
        cv2.imwrite(output_image, frame)
        print(f"Successfully saved the last frame as {output_image}")
        cap.release()
    else:
        cap.release()
        set_process(f"[error]cv2延长视频末尾失败")
        return
    # 读取 源视频的帧率
    cap = cv2.VideoCapture(novoice_mp4)
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()

    # 生成 设定时间的片段mp4
    clip_video = f"{folder_path}/{time.time()}.mp4"
    # 计算生成视频的帧数
    frame_count_new = int(fps * duration_ms / 1000)
    # 用相同的帧生成  clip
    image = cv2.imread(output_image)
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    video_writer = cv2.VideoWriter(clip_video, fourcc, fps, (width, height))
    for _ in range(frame_count_new):
        video_writer.write(image)
    video_writer.release()
    # 连接源视频和该片段
    runffmpeg(
        f'-y -i "{novoice_mp4}" -i "{clip_video}" -filter_complex "[0:v]setsar=1[v0];[1:v]setsar=1[v1];[v0][v1]concat=n=2:v=1:a=0[outv]" -map "[outv]" -c:v libx264 -y "{novoice_mp4}-tmp.mp4"')
    # os.rename(novoice_mp4,novoice_mp4+'.raw.mp4')
    # os.rename(novoice_mp4+"-tmp.mp4",novoice_mp4)

def add_clip_to_last_ff(noextname,duration_ms):
    folder_path = config.rootdir + f'/tmp/{noextname}'
    novoice_mp4 = f"{folder_path}/novoice.mp4"
    # 生成 设定时间的片段mp4
    clip_video = f"{folder_path}/{time.time()}.mp4"
    tmp_video = f"{folder_path}/{time.time()}-tmp.mp4"
    total_length = get_video_duration(novoice_mp4)
    runffmpeg([
        "-y",
        "-i",
        f'"{novoice_mp4}"',
        "-ss",
        ms_to_time_string(ms=total_length-1000).replace(',','.'),
        "-t",
        "1",
        f'{clip_video}'
    ])
    if duration_ms<=1000:
        tmp_video=clip_video
    else:
        pts=duration_ms/1000
        runffmpeg(
            f'-y  -i "{clip_video}" -vf "setpts={pts}*PTS" -c:v libx264  -an "{tmp_video}"'
        )

    runffmpeg(
        f'-y -i "{novoice_mp4}" -i "{tmp_video}" -filter_complex "[0:v]setsar=1[v0];[1:v]setsar=1[v1];[v0][v1]concat=n=2:v=1:a=0[outv]" -map "[outv]" -c:v libx264 -y "{novoice_mp4}-tmp.mp4"')
    return



# 执行tts并行
def exec_tts(queue_tts, total_length, noextname):
    total_length = total_length * 1000
    queue_copy = copy.deepcopy(queue_tts)
    set_process(f"准备进行 {config.video['tts_type']} 语音合成，角色:{config.video['voice_role']}", 'logs')

    def get_item(q):
        return {"text": q['text'], "role": q['role'], "rate": q['rate'], "filename": q["filename"],
                "tts_type": config.video['tts_type']}

    # 需要并行的数量3
    while len(queue_tts) > 0:
        if config.current_status != 'ing':
            return
        tolist = [threading.Thread(target=text_to_speech, kwargs=get_item(queue_tts.pop(0)))]
        if len(queue_tts) > 0:
            tolist.append(threading.Thread(target=text_to_speech, kwargs=get_item(queue_tts.pop(0))))
        if len(queue_tts) > 0:
            tolist.append(threading.Thread(target=text_to_speech, kwargs=get_item(queue_tts.pop(0))))

        for t in tolist:
            t.start()
        for t in tolist:
            t.join()
    segments = []
    start_times = []
    # 如果设置了视频自动降速
    if config.video['video_autorate']:
        return video_autorate_process(noextname, queue_copy, total_length)
    try:
        # 偏移时间，用于每个 start_time 增减
        offset = 0
        for (idx, it) in enumerate(queue_copy):
            set_process(f"<br>befor: {it['startraw']=},{it['endraw']=}")
            it['start_time'] += offset
            it['end_time'] += offset
            it['startraw'] = ms_to_time_string(ms=it['start_time'])
            it['endraw'] = ms_to_time_string(ms=it['end_time'])
            if not os.path.exists(it['filename']) or os.path.getsize(it['filename']) == 0:
                start_times.append(it['start_time'])
                segments.append(AudioSegment.silent(duration=it['end_time'] - it['start_time']))
                set_process(f"[error]: 此 {it['startraw']} - {it['endraw']} 时间段内字幕合成语音失败", 'logs')
                queue_copy[idx]=it
                continue
            audio_data = AudioSegment.from_file(it['filename'], format="mp3")

            # 原字幕发音时间段长度
            wavlen = it['end_time'] - it['start_time']
            # 新发音长度 为 0,则忽略掉
            if wavlen == 0:
                queue_copy[idx]=it
                continue
            # 新配音时长
            mp3len = len(audio_data)
            # 新配音大于原时长
            diff = mp3len - wavlen
            set_process(f"{diff=},{mp3len=},{wavlen=}")
            if diff>0 and config.video['voice_autorate']:
                speed = mp3len / wavlen
                speed = 1.8 if speed > 1.8 else speed
                # 新的长度
                mp3len = mp3len / speed
                diff=mp3len - wavlen
                if diff<0:
                    diff=0
                set_process(f"自动加速配音 {speed} 倍<br>")
                # 音频加速 最大加速2倍
                audio_data = speed_change(audio_data, speed)
                # 增加新的偏移
                offset+=diff
            elif diff>0:
                offset+=diff
            set_process(f"new-offset={offset}")
            it['end_time'] = it['start_time'] + mp3len
            it['startraw'] = ms_to_time_string(ms=it['start_time'])
            it['endraw'] = ms_to_time_string(ms=it['end_time'])
            queue_copy[idx] = it
            set_process(f"after: {it['startraw']=},{it['endraw']=}")

            start_times.append(it['start_time'])
            segments.append(audio_data)

        # 更新字幕
        srt = ""
        for (idx, it) in enumerate(queue_copy):
            srt += f"{idx + 1}\n{it['startraw']} --> {it['endraw']}\n{it['text']}\n\n"
        sub_name = config.rootdir + f"/tmp/{noextname}/{noextname}.srt"
        with open(sub_name, 'w', encoding="utf-8") as f:
            f.write(srt.strip())
        if offset > 0 and queue_copy[-1]['end_time']>total_length:
            # 判断 最后一个片段的 end_time 是否超出 total_length,如果是 ，则修改offset，增加
            offset=queue_copy[-1]['end_time']-total_length
            set_process(f"{offset=}>0，需要末尾添加延长视频帧 {offset}秒")
            try:
                add_clip_to_last(noextname,offset)
                folder_path = config.rootdir + f'/tmp/{noextname}'
                novoice_mp4 = f"{folder_path}/novoice.mp4"
                if os.path.exists(novoice_mp4 + "-tmp.mp4") and os.path.getsize(novoice_mp4 + "-tmp.mp4")>0:
                    os.rename(novoice_mp4, novoice_mp4 + '.raw.mp4')
                    os.rename(novoice_mp4 + "-tmp.mp4", novoice_mp4)
                    t=get_video_duration(novoice_mp4)
                    if t is not None and t > total_length:
                        # 如果新的视频长度大于音频长度，则差为新的偏移，更新offset
                        offset=t-total_length
                else:
                    offset=0
            except Exception as e:
                set_process(f"[error]末尾添加延长视频帧失败，将保持原样，截断音频:{str(e)}")
                offset=0
        merge_audio_segments(segments, start_times, total_length + offset, noextname)

    except Exception as e:
        set_process(f"[error] exec_tts 合成语音有出错:" + str(e))


# 最终合成视频 source_mp4=原始mp4视频文件，noextname=无扩展名的视频文件名字
def compos_video(source_mp4, noextname):
    folder_path = config.rootdir + f'/tmp/{noextname}'
    sub_name = f"{folder_path}/{noextname}.srt"
    # 保存字幕
    if os.path.exists(sub_name):
        shutil.copy(sub_name, config.video['target_dir'] + f"/{noextname}/{config.video['target_language']}.srt")
    tts_wav = f"{folder_path}/tts-{noextname}.wav"
    source_wav = f"{folder_path}/{noextname}.wav"
    # target  output mp4 filepath
    target_mp4 = f"{config.video['target_dir']}/{noextname}.mp4"
    set_process(f"合并后将创建到 {target_mp4}")
    # 预先创建好的
    novoice_mp4 = f"{folder_path}/novoice.mp4"
    # 判断novoice_mp4是否完成
    # 判断novoice_mp4是否完成
    if not is_novoice_mp4(novoice_mp4, noextname):
        return
        # while True:
    #     if config.current_status != 'ing':
    #         return
    #     if noextname not in config.queue_novice:
    #         msg = f"抱歉，视频{noextname} 预处理 novoice 失败,请重试"
    #         set_process(msg)
    #         raise Exception(msg)
    #     if config.queue_novice[noextname] == 'error':
    #         msg = f"抱歉，视频{noextname} 预处理 novoice 失败"
    #         set_process(msg)
    #         raise Exception(msg)
    #
    #     if config.queue_novice[noextname] == 'ing':
    #         set_process(f"{noextname} 所需资源未准备完毕，请稍等..{config.queue_novice[noextname]=}")
    #         time.sleep(3)
    #         continue
    #     break

    # 需要配音
    if config.video['voice_role'] != 'No':
        if not os.path.exists(tts_wav) or os.path.getsize(tts_wav) == 0:
            set_process(f"[error] 配音文件创建失败: {tts_wav}", 'logs')
            return
    # 需要字幕
    if config.video['subtitle_type'] > 0 and (not os.path.exists(sub_name) or os.path.getsize(sub_name) == 0):
        set_process(f"[error]未创建成功有效的字幕文件 {sub_name}", 'logs')
        return
    if config.video['subtitle_type'] == 1:
        # 硬字幕
        # 重新整理字幕，换行
        subs = get_subtitle_from_srt(sub_name)
        maxlen = 36 if config.video['target_language'][:2] in ["zh", "ja", "jp", "ko"] else 80
        subtitles = ""
        for it in subs:
            it['text'] = textwrap.fill(it['text'], maxlen)
            subtitles += f"{it['line']}\n{it['time']}\n{it['text']}\n\n"
        with open(sub_name, 'w', encoding="utf-8") as f:
            f.write(subtitles.strip())
        hard_srt=sub_name.replace('\\','/').replace(':','\\:')
    # 有字幕有配音
    if config.video['voice_role'] != 'No' and config.video['subtitle_type'] > 0:
        if config.video['subtitle_type'] == 1:
            set_process(f"{noextname} 合成配音+硬字幕")
            # 需要配音+硬字幕
            runffmpeg([
                "-y",
                "-i",
                f'"{novoice_mp4}"',
                "-i",
                f'"{tts_wav}"',
                "-c:v",
                "libx264",
                # "libx264",
                "-c:a",
                "aac",
                # "pcm_s16le",
                "-vf",
                f"\"subtitles='{hard_srt}'\"",
                # "-shortest",
                f'"{target_mp4}"'
            ])
        else:
            set_process(f"{noextname} 合成配音+软字幕")
            # 配音+软字幕
            runffmpeg([
                "-y",
                "-i",
                f'"{novoice_mp4}"',
                "-i",
                f'"{tts_wav}"',
                "-sub_charenc",
                "UTF-8",
                "-f",
                "srt",
                "-i",
                f'"{sub_name}"',
                "-c:v",
                "libx264",
                # "libx264",
                "-c:a",
                "aac",
                "-c:s",
                "mov_text",
                "-metadata:s:s:0",
                f"language={config.video['subtitle_language']}",
                # "-shortest",
                f'"{target_mp4}"'
            ])
    elif config.video['voice_role'] != 'No':
        # 配音无字幕
        set_process(f"{noextname} 合成配音，无字幕")
        runffmpeg([
            "-y",
            "-i",
            f'"{novoice_mp4}"',
            "-i",
            f'"{tts_wav}"',
            "-c:v",
            "copy",
            # "libx264",
            "-c:a",
            "aac",
            # "pcm_s16le",
            # "-shortest",
            f'"{target_mp4}"'
        ])
    # 无配音 使用 novice.mp4 和 原始 wav合并
    elif config.video['subtitle_type'] == 1:
        # 硬字幕无配音 将原始mp4复制到当前文件夹下
        set_process(f"{noextname} 合成硬字幕，无配音")
        runffmpeg([
            "-y",
            "-i",
            f'"{novoice_mp4}"',
            "-i",
            f'"{source_wav}"',
            "-c:v",
            "libx264",
            # "libx264",
            "-c:a",
            "aac",
            # "pcm_s16le",
            "-vf",
            f"\"subtitles='{hard_srt}'\"",
            # "-shortest",
            f'"{target_mp4}"',
        ])
    elif config.video['subtitle_type'] == 2:
        # 软字幕无配音
        set_process(f"{noextname} 合成软字幕，无配音")
        runffmpeg([
            "-y",
            "-i",
            f'"{novoice_mp4}"',
            "-i",
            f'"{source_wav}"',
            "-sub_charenc",
            "UTF-8",
            "-f",
            "srt",
            "-i",
            f'"{sub_name}"',
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            # "libx264",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            f"language={config.video['subtitle_language']}",
            # "-shortest",
            f'"{target_mp4}"'
        ])
    set_process(f"{noextname} 视频合成完毕")


# 写入日志队列
def set_process(text, type="logs"):
    try:
        if text:
            log_msg=text.replace('<br>', "\n").strip()
            if log_msg.startswith("[error"):
                logger.error(log_msg)
            else:
                logger.info(log_msg)
        if type=='logs':
            text=text.replace('[error]', '<strong style="color:#f00">出错:</strong>')+'<br>'
        queue_logs.put_nowait({"text": text, "type": type})
    except:
        pass


def is_vlc():
    try:
        if find_lib() is None:
            config.is_vlc = False
        else:
            config.is_vlc = True
    except:
        config.is_vlc = False

def delete_files(directory,ext):
    # 获取目录下的所有文件和子目录
    files_and_dirs = os.listdir(directory)

    # 遍历文件和子目录
    for item in files_and_dirs:
        item_path = os.path.join(directory, item)
        # 如果是文件，且是 mp3 文件，删除之
        if os.path.isfile(item_path) and item.lower().endswith(ext):
            os.remove(item_path)
            print(f"Deleted: {item_path}")

        # 如果是子目录，递归调用删除函数
        elif os.path.isdir(item_path):
            delete_files(item_path)