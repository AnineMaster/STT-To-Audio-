import gradio as gr
import edge_tts
import asyncio
import nest_asyncio
import re
import tempfile
import os
import io
from pydub import AudioSegment

# Jupyter / Colab environments me async loops enable karne ke liye
nest_asyncio.apply()

VOICE_DICT = {}

async def load_all_voices():
    global VOICE_DICT
    try:
        voices = await edge_tts.list_voices()
        VOICE_DICT = {f"{v['Locale']} | {v['ShortName']} ({v['Gender']})": v['ShortName'] for v in voices}
        
        # Indian voices ko list me sabse upar layout karne ke liye filter
        indian_voices = [k for k in VOICE_DICT.keys() if "-IN" in k]
        other_voices = [k for k in VOICE_DICT.keys() if "-IN" not in k]
        
        return sorted(indian_voices) + sorted(other_voices)
    except Exception as e:
        return [f"Error loading voices: {str(e)}"]

def time_to_ms(time_str):
    h, m, s_ms = time_str.split(':')
    s, ms = s_ms.split(',')
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int(ms)

def parse_srt_with_timings(srt_content):
    segments = []
    blocks = re.split(r'\n\s*\n', srt_content.strip())
    for block in blocks:
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if len(lines) < 3: continue
        try:
            timing_line = next(l for l in lines if '-->' in l)
            start_time_str, end_time_str = timing_line.split(' --> ')
            start_ms = time_to_ms(start_time_str)
            end_ms = time_to_ms(end_time_str)
            idx = lines.index(timing_line)
            text = " ".join(lines[idx+1:])
            if text: segments.append({'start': start_ms, 'end': end_ms, 'text': text})
        except Exception: continue
    return sorted(segments, key=lambda x: x['start'])

def parse_speaker_and_text(text):
    m1 = re.match(r'^\[([^\]]+)\]\s*:\s*(.*)$', text, re.DOTALL)
    if m1: return m1.group(1).strip(), m1.group(2).strip()
    
    m2 = re.match(r'^\[([^\]]+)\]\s*(.*)$', text, re.DOTALL)
    if m2: return m2.group(1).strip(), m2.group(2).strip()
        
    m3 = re.match(r'^([^:\n]+)\s*:\s*(.*)$', text, re.DOTALL)
    if m3 and len(m3.group(1).strip()) < 25:
        return m3.group(1).strip(), m3.group(2).strip()
            
    return None, text

def parse_speaker_mappings(mapping_text, voice_dict):
    mappings = {}
    if not mapping_text: return mappings
    for line in mapping_text.strip().split('\n'):
        if '=' in line: key, val = line.split('=', 1)
        elif ':' in line: key, val = line.split(':', 1)
        else: continue
        
        key, val = key.strip(), val.strip()
        matched_voice = None
        if val in voice_dict.values():
            matched_voice = val
        else:
            for k, v in voice_dict.items():
                if val in k or val in v:
                    matched_voice = v
                    break
        if matched_voice: 
            mappings[key] = matched_voice
    return mappings

async def generate_tts_with_retry(text, voice_name, rate_str, pitch_str, output_path, retries=3):
    for i in range(retries):
        try:
            communicate = edge_tts.Communicate(text, voice_name, rate=rate_str, pitch=pitch_str)
            await communicate.save(output_path)
            return True
        except Exception as e:
            if i == retries - 1: return False
            await asyncio.sleep(2)
    return False

async def convert_srt_to_tts_wrapper(
    srt_input_type, 
    srt_file_obj, 
    srt_text_input, 
    voice_selection, 
    rate, 
    pitch, 
    speaker_mappings_text, 
    export_format, 
    sample_rate, 
    bitrate, 
    progress=gr.Progress()
):
    srt_content = ""
    if srt_input_type == "Upload File" and srt_file_obj:
        with open(srt_file_obj.name, 'r', encoding='utf-8') as f: srt_content = f.read()
    elif srt_input_type == "Paste Text": srt_content = srt_text_input

    if not srt_content.strip(): return "Invalid input", None
    segments = parse_srt_with_timings(srt_content)
    if not segments: return "No segments found", None

    speaker_mappings = parse_speaker_mappings(speaker_mappings_text, VOICE_DICT)
    
    combined_audio = AudioSegment.silent(duration=0, frame_rate=sample_rate)
    default_voice_name = VOICE_DICT[voice_selection]
    rate_str = f"{rate:+d}%"
    pitch_str = f"{pitch:+d}Hz"

    for segment in progress.tqdm(segments, desc="Syncing High Quality Audio"):
        start_ms = segment['start']
        end_ms = segment['end']
        text = segment['text']

        speaker_id, clean_text = parse_speaker_and_text(text)
        assigned_voice = default_voice_name
        if speaker_id and speaker_id in speaker_mappings:
            assigned_voice = speaker_mappings[speaker_id]

        if len(combined_audio) < start_ms:
            silence_before = start_ms - len(combined_audio)
            combined_audio += AudioSegment.silent(duration=silence_before, frame_rate=sample_rate)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            success = await generate_tts_with_retry(clean_text, assigned_voice, rate_str, pitch_str, tmp.name)
            if success:
                seg_audio = AudioSegment.from_file(tmp.name).set_frame_rate(sample_rate)
                combined_audio += seg_audio

                if len(combined_audio) < end_ms:
                    silence_after = end_ms - len(combined_audio)
                    combined_audio += AudioSegment.silent(duration=silence_after, frame_rate=sample_rate)

            os.remove(tmp.name)
        await asyncio.sleep(0.05)

    output_path = f"final_audio.{export_format}"
    
    if export_format in ["wav", "flac"]:
        combined_audio.export(output_path, format=export_format)
    else:
        combined_audio.export(output_path, format=export_format, bitrate=bitrate)
        
    return f"Success: Audio generated in {export_format.upper()} format.", output_path

# Event loop to fetch voice models
try:
    loop = asyncio.get_event_loop()
    voice_list = loop.run_until_complete(load_all_voices())
except: 
    voice_list = []

# Customized Green Layout
with gr.Blocks(theme=gr.themes.Soft(primary_hue="green", secondary_hue="emerald")) as demo:
    gr.Markdown("### 🎙️ Sync SRT to Audio (Multilingual & Multiple Speakers)")
    
    with gr.Tab("SRT to Audio Engine"):
        with gr.Row():
            with gr.Column(scale=2):
                srt_input_choice = gr.Radio(["Upload File", "Paste Text"], value="Upload File", label="Input Type")
                file_input = gr.File(label="SRT File")
                srt_text_area = gr.Textbox(visible=False, label="Paste SRT Content", lines=10)
                voice_dropdown = gr.Dropdown(choices=voice_list, value=voice_list[0] if voice_list else None, label="Voice")
                with gr.Row():
                    rate_slider = gr.Slider(-50, 50, 0, label="Base Speed (%)")
                    pitch_slider = gr.Slider(-50, 50, 0, label="Pitch (Hz)")
            
            with gr.Column(scale=1):
                gr.Markdown("#### 👥 Speaker Setup (No Limits)")
                speaker_mapping_input = gr.Textbox(
                    label="Assign Voices to Speaker Tags",
                    placeholder="Speaker 1: hi-IN-MadhurNeural\nSpeaker 2: en-US-GuyNeural",
                    lines=5
                )
                
                gr.Markdown("#### ⚙️ Export Configuration")
                export_format_drop = gr.Dropdown(choices=["mp3", "wav", "ogg", "flac"], value="mp3", label="Format")
                sample_rate_drop = gr.Dropdown(choices=[22050, 44100, 48000], value=44100, label="Sample Rate (Hz)")
                bitrate_drop = gr.Dropdown(choices=["96k", "128k", "192k", "256k", "320k"], value="192k", label="Bitrate (For MP3/OGG)")
                
        btn = gr.Button("Generate Multi-Speaker Synced Audio", variant="primary")
        status = gr.Textbox(label="Status")
        audio_out = gr.Audio(label="Output Audio")

        srt_input_choice.change(
            lambda c: (gr.File(visible=c=="Upload File"), gr.Textbox(visible=c=="Paste Text")), 
            inputs=srt_input_choice, 
            outputs=[file_input, srt_text_area]
        )
        
        btn.click(
            convert_srt_to_tts_wrapper, 
            inputs=[
                srt_input_choice, 
                file_input, 
                srt_text_area, 
                voice_dropdown, 
                rate_slider, 
                pitch_slider, 
                speaker_mapping_input,
                export_format_drop,
                sample_rate_drop,
                bitrate_drop
            ], 
            outputs=[status, audio_out]
        )

# Gradio deployment with share & debug active
demo.launch(share=True, debug=True)
