import os
import argparse
from concurrent.futures import ThreadPoolExecutor

from moviepy.editor import AudioFileClip

audio_dir = "./raw_audio/"

def generate_infos(filelist):
    videos = []
    for file in filelist:
        if file.endswith(".mp4"):
            videos.append(file)
    return videos


def clip_file(file):
    my_audio_clip = AudioFileClip(video_dir + file)
    my_audio_clip.write_audiofile(audio_dir + file.rstrip("mp4") + "wav")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--video_dir", default='/kaggle/input/video-data/')
    args = parser.parse_args()
    
    video_dir = args.video_dir
    filelist = list(os.walk(video_dir))[0][2]
    infos = generate_infos(filelist)
    
    with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        executor.map(clip_file, infos)
