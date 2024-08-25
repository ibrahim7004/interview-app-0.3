from flask import Flask, render_template, request, redirect, url_for, send_file, jsonify
import pyaudio
import wave
import struct
import math
import tempfile
import os
from openai import OpenAI
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import re

load_dotenv()

app = Flask(__name__)

openai_api_key = os.getenv('OPENAI_API_KEY')
client = OpenAI(api_key=openai_api_key)

# Constants for recording
RATE = 16000
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1
Threshold = 10
TIMEOUT_LENGTH = 2.3
SHORT_NORMALIZE = (1.0 / 32768.0)
swidth = 2

class Recorder:
    @staticmethod
    def rms(frame):
        count = len(frame) // swidth
        format = "%dh" % count
        shorts = struct.unpack(format, frame)

        sum_squares = 0.0
        for sample in shorts:
            n = sample * SHORT_NORMALIZE
            sum_squares += n * n
        rms = math.pow(sum_squares / count, 0.5)
        return rms * 1000

    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.stream = self.p.open(format=FORMAT,
                                  channels=CHANNELS,
                                  rate=RATE,
                                  input=True,
                                  frames_per_buffer=CHUNK)

    def record(self):
        rec = []
        silent_chunks = 0
        max_silent_chunks = int(TIMEOUT_LENGTH * RATE / CHUNK)

        while True:
            data = self.stream.read(CHUNK)
            rec.append(data)

            if self.rms(data) < Threshold:
                silent_chunks += 1
            else:
                silent_chunks = 0

            if silent_chunks > max_silent_chunks:
                break

        return b''.join(rec)

    def listen(self):
        while True:
            input_data = self.stream.read(CHUNK)
            if self.rms(input_data) > Threshold:
                audio_data = self.record()
                return audio_data

def save_audio(recorder, audio_data, file_path):
    with wave.open(file_path, 'wb') as wave_file:
        wave_file.setnchannels(CHANNELS)
        wave_file.setsampwidth(recorder.p.get_sample_size(FORMAT))
        wave_file.setframerate(RATE)
        wave_file.writeframes(audio_data)


def transcribe_audio(file_path):
    with open(file_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
        model="whisper-1", 
        file=audio_file
        )
    return transcription.text
    
    
def convert_string(content):
    try:
        result = int(content)
    except ValueError:
        match = re.search(r'\d+', content)
        if match:
            result = int(match.group())
        else:
            result = 5
    return result

def score(question, answer):
    prompt = (
            f"Score the given answer to the following question out of 10: "
            f"4 points for Accuracy, 3 points for Comprehensiveness, and 3 points for Clarity and Communication. "
            f"Be strict but fair about the scoring. Return a single number as the score e.g: 2. "
            f"If there is no answer just give 0."
            f"Input: Question: {question} Answer: {answer}"
    )
    message = [
        {"role": "system", "content": "You are a helpful assistant that scores answer, based on given question. You return only an integer between 1-10"},
        {"role": "user", "content": prompt}
    ]

    response = client.chat.completions.create(
        model="gpt-4",
        messages=message
    )
    score = response.choices[0].message.content
    return score


# Google Sheets constants
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SERVICE_ACCOUNT_FILE = 'sorsx-sheets-3f5a6f7866c5.json'
SPREADSHEET_ID = '18iwFkn6PasXuckVZxUc3DgCW9OohTs0kjcqGibrPhO4'
COLUMN_RANGE = 'Sheet1!A:B'

def authenticate_google_sheets():
    """Authenticate and return the Google Sheets service instance."""
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build('sheets', 'v4', credentials=creds)

def find_next_empty_row(service):
    """Find the next empty row in the specified column range."""
    sheet = service.spreadsheets()
    result = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range=COLUMN_RANGE).execute()
    return len(result.get('values', [])) + 1

def write_to_google_sheet(service, username, score):
    """Write the username and score to the next empty row in the Google Sheet."""
    next_empty_row = find_next_empty_row(service)
    range_name = f'Sheet1!A{next_empty_row}:B{next_empty_row}'
    values = [[username, score]]
    body = {'values': values}
    result = service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID, range=range_name,
        valueInputOption='RAW', body=body).execute()

@app.route("/finalize-interview", methods=["POST"])
def finalize_interview():
    username = request.form.get("username")
    total_score = float(request.form.get("total_score"))
    
    service = authenticate_google_sheets()
    write_to_google_sheet(service, username, total_score)
    
    return jsonify({"message": "Interview finalized and score saved to Google Sheets."})

@app.route('/score-answer', methods=['POST'])
def score_answer():
    question = request.form.get('question')
    answer = request.form.get('answer')
    score_result = score(question, answer)
    return jsonify({"score": float(score_result)})


@app.route("/start-recording", methods=["POST"])
def start_recording():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
        file_path = tmp_file.name

    recorder = Recorder()
    audio_data = recorder.listen()
    save_audio(recorder, audio_data, file_path)
    transcription = transcribe_audio(file_path)
    os.remove(file_path)

    return jsonify({"transcription": transcription})

@app.route("/")
def home():
    return redirect(url_for("dashboard"))


@app.route("/generate-tts", methods=["POST"])
def generate_tts():
    text = request.form.get('text')
    voice = 'nova'

    with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as tmp_file:
        file_path = tmp_file.name

    with client.audio.speech.with_streaming_response.create(
        model="tts-1",
        voice=voice,
        speed=0.9,
        input=text 
    ) as response:
        response.stream_to_file(file_path)

    return send_file(file_path, mimetype='audio/wav')


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html", first_name="To", last_name="AI Interviewer")


@app.route("/meeting")
def meeting():
    app_id = os.getenv('ZEGO_APPID')
    server_secret = os.getenv('ZEGO_SERVER_SECRET')
    return render_template("meeting.html", username=" ", app_id=app_id, server_secret=server_secret)


@app.route("/join", methods=["GET", "POST"])
def join():
    if request.method == "POST":
        room_id = request.form.get("roomID")
        return redirect(f"/meeting?roomID={room_id}")
    return render_template("join.html")

if __name__ == "__main__":
    app.run(debug=True)
