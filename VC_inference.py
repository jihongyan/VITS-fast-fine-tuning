import os
import numpy as np
import torch
from torch import no_grad, LongTensor
import argparse
import commons
from mel_processing import spectrogram_torch
import utils
from models import SynthesizerTrn
import gradio as gr
import librosa
import whisper
from whisper.utils import get_writer
from gradio import processing_utils
from zhconv import convert

from text import text_to_sequence, _clean_text
device = "cuda:1" if torch.cuda.is_available() else "cpu"
import logging
logging.getLogger("PIL").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("markdown_it").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

language_marks = {
    "Japanese": "",
    "日本語": "[JA]",
    "简体中文": "[ZH]",
    "English": "[EN]",
    "Mix": "",
}
lang = ['日本語', '简体中文', 'English', 'Mix']
def get_text(text, hps, is_symbol):
    text_norm = text_to_sequence(text, hps.symbols, [] if is_symbol else hps.data.text_cleaners)
    if hps.data.add_blank:
        text_norm = commons.intersperse(text_norm, 0)
    text_norm = LongTensor(text_norm)
    return text_norm

# 秒转时分秒毫秒
def seconds_to_hmsm(seconds):
    hours = str(int(seconds // 3600))
    minutes = str(int((seconds % 3600) // 60))
    seconds = seconds % 60
    milliseconds = str(int(int((seconds - int(seconds)) * 1000))) # 毫秒留三位
    seconds = str(int(seconds))
    # 补0
    if len(hours) < 2:
        hours = '0' + hours
    if len(minutes) < 2:
        minutes = '0' + minutes
    if len(seconds) < 2:
        seconds = '0' + seconds
    if len(milliseconds) < 3:
        milliseconds = '0'*(3-len(milliseconds)) + milliseconds
    return f"{hours}:{minutes}:{seconds},{milliseconds}"

def create_tts_fn(model, hps, speaker_ids):
    def tts_fn(text, speaker, language, speed):
        if language is not None:
            textAndMark = language_marks[language] + text + language_marks[language]
        speaker_id = speaker_ids[speaker]
        stn_tst = get_text(textAndMark, hps, False)
        with no_grad():
            x_tst = stn_tst.unsqueeze(0).to(device)
            x_tst_lengths = LongTensor([stn_tst.size(0)]).to(device)
            sid = LongTensor([speaker_id]).to(device)
            audio = model.infer(x_tst, x_tst_lengths, sid=sid, noise_scale=.667, noise_scale_w=0.8,
                                length_scale=1.0 / speed)[0][0, 0].data.cpu().float().numpy()
        del stn_tst, x_tst, x_tst_lengths, sid
        
        audio_path = './outputs/output_audio.wav'
        #processing_utils.audio_to_file(hps.data.sampling_rate, audio, audio_path, format="wav")

        whisper_model = whisper.load_model('large')
        options = dict(beam_size=5, best_of=5, word_timestamps=True)
        transcribe_options = dict(task="transcribe", **options)
        result = whisper_model.transcribe(audio, fp16=False, language='Chinese', initial_prompt=text, **transcribe_options)
        
        # 结果可能是繁体，转为简体zh-cn
        for i in range(len(result['segments'])):
            result['segments'][i]['text'] = convert(result['segments'][i]['text'], 'zh-cn')
        
        # 写入字幕文件
        writer = get_writer("srt", './outputs')
        writer_args = {"max_line_width":8, "max_line_count":1}
        #writer_args = {"max_words_per_line":6}
        writer(result, audio_path, **writer_args)
        
        srt_file = "./outputs/output_audio.srt"
        # with open(srt_file, 'w', encoding='utf-8') as f:
            # i = 1
            # for r in result['segments']:
	            # f.write(str(i)+'\n')
	            # f.write(seconds_to_hmsm(float(r['start']))+' --> '+seconds_to_hmsm(float(r['end']))+'\n')
	            # i += 1
	            # f.write(convert(r['text'], 'zh-cn')+'\n') # 结果可能是繁体，转为简体zh-cn
	            # f.write('\n')

        return "Success", srt_file, (hps.data.sampling_rate, audio)

    return tts_fn

def create_vc_fn(model, hps, speaker_ids):
    def vc_fn(original_speaker, target_speaker, record_audio, upload_audio):
        input_audio = record_audio if record_audio is not None else upload_audio
        if input_audio is None:
            return "You need to record or upload an audio", None
        sampling_rate, audio = input_audio
        original_speaker_id = speaker_ids[original_speaker]
        target_speaker_id = speaker_ids[target_speaker]

        audio = (audio / np.iinfo(audio.dtype).max).astype(np.float32)
        if len(audio.shape) > 1:
            audio = librosa.to_mono(audio.transpose(1, 0))
        if sampling_rate != hps.data.sampling_rate:
            audio = librosa.resample(audio, orig_sr=sampling_rate, target_sr=hps.data.sampling_rate)
        with no_grad():
            y = torch.FloatTensor(audio)
            y = y / max(-y.min(), y.max()) / 0.99
            y = y.to(device)
            y = y.unsqueeze(0)
            spec = spectrogram_torch(y, hps.data.filter_length,
                                     hps.data.sampling_rate, hps.data.hop_length, hps.data.win_length,
                                     center=False).to(device)
            spec_lengths = LongTensor([spec.size(-1)]).to(device)
            sid_src = LongTensor([original_speaker_id]).to(device)
            sid_tgt = LongTensor([target_speaker_id]).to(device)
            audio = model.voice_conversion(spec, spec_lengths, sid_src=sid_src, sid_tgt=sid_tgt)[0][
                0, 0].data.cpu().float().numpy()
        del y, spec, spec_lengths, sid_src, sid_tgt
        return "Success", (hps.data.sampling_rate, audio)

    return vc_fn
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_dir", default="./G_latest.pth", help="directory to your fine-tuned model")
    parser.add_argument("--config_dir", default="./finetune_speaker.json", help="directory to your model config file")
    parser.add_argument("--share", default=False, help="make link public (used in colab)")

    args = parser.parse_args()
    hps = utils.get_hparams_from_file(args.config_dir)


    net_g = SynthesizerTrn(
        len(hps.symbols),
        hps.data.filter_length // 2 + 1,
        hps.train.segment_size // hps.data.hop_length,
        n_speakers=hps.data.n_speakers,
        **hps.model).to(device)
    _ = net_g.eval()

    _ = utils.load_checkpoint(args.model_dir, net_g, None)
    speaker_ids = hps.speakers
    speakers = list(hps.speakers.keys())
    tts_fn = create_tts_fn(net_g, hps, speaker_ids)
    vc_fn = create_vc_fn(net_g, hps, speaker_ids)
    app = gr.Blocks()
    with app:
        with gr.Tab("Text-to-Speech"):
            with gr.Row():
                with gr.Column():
                    textbox = gr.TextArea(label="Text",
                                          placeholder="Type your sentence here",
                                          value="こんにちわ。", elem_id=f"tts-input")
                    # select character
                    char_dropdown = gr.Dropdown(choices=speakers, value=speakers[0], label='character')
                    language_dropdown = gr.Dropdown(choices=lang, value=lang[0], label='language')
                    duration_slider = gr.Slider(minimum=0.1, maximum=5, value=1, step=0.1,
                                                label='速度 Speed')
                with gr.Column():
                    text_output = gr.Textbox(label="Message")
                    srt_output = gr.File(label="Srt字幕文件")
                    audio_output = gr.Audio(label="Output Audio", elem_id="tts-audio")
                    btn = gr.Button("Generate!")
                    btn.click(tts_fn,
                              inputs=[textbox, char_dropdown, language_dropdown, duration_slider,],
                              outputs=[text_output, srt_output, audio_output])
        with gr.Tab("Voice Conversion"):
            gr.Markdown("""
                            录制或上传声音，并选择要转换的音色。
            """)
            with gr.Column():
                record_audio = gr.Audio(label="record your voice", sources="microphone")
                upload_audio = gr.Audio(label="or upload audio here", sources="upload")
                source_speaker = gr.Dropdown(choices=speakers, value=speakers[0], label="source speaker")
                target_speaker = gr.Dropdown(choices=speakers, value=speakers[0], label="target speaker")
            with gr.Column():
                message_box = gr.Textbox(label="Message")
                converted_audio = gr.Audio(label='converted audio')
            btn = gr.Button("Convert!")
            btn.click(vc_fn, inputs=[source_speaker, target_speaker, record_audio, upload_audio],
                      outputs=[message_box, converted_audio])

    app.launch(share=args.share,server_port=7861)

