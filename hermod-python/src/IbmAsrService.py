""" IBM based speech recognition service """
import time
import json
import collections
import os
import os.path
import asyncio
import base64
import websockets
import pyaudio
import webrtcvad
from dotenv import load_dotenv
from MqttService import MqttService
from io_buffer import BytesLoop
# ibm

CHUNK = 1024
FORMAT = pyaudio.paInt16
# Even if your default input is multi channel (like a webcam mic),
# it's really important to only record 1 channel, as the STT service
# does not do anything useful with stereo. You get a lot of "hmmm"
# back.
CHANNELS = 1
# Rate is important, nothing works without it. This is a pretty
# standard default. If you have an audio device that requires
# something different, change this.
RATE = 44100
RECORD_SECONDS = 10
FINALS = []

load_dotenv()

def get_region_map():
    """get map of region codes to transcription services"""
    return {
        'us-east': 'gateway-wdc.watsonplatform.net',
        'us-south': 'stream.watsonplatform.net',
        'eu-gb': 'stream.watsonplatform.net',
        'eu-de': 'stream-fra.watsonplatform.net',
        'au-syd': 'gateway-syd.watsonplatform.net',
        'jp-tok': 'gateway-syd.watsonplatform.net',
    }


def get_url():
    """get url for transcription service based on env vars"""
    # if region is set, use lookups
    # https://console.bluemix.net/docs/services/speech-to-text/websockets.html#websockets
    if os.environ.get('IBM_SPEECH_TO_TEXT_REGION', False):
        host = get_region_map().get(os.environ.get('IBM_SPEECH_TO_TEXT_REGION'))
        return ("wss://{}/speech-to-text/api/v1/recognize" \
        + "?model=en-US_BroadbandModel").format(host)
    # if url from downloaded creds
    elif os.environ.get('IBM_SPEECH_TO_TEXT_URL', False):
        return os.environ.get('IBM_SPEECH_TO_TEXT_URL')
    # fallback to us-east
    else:
        return ("wss://{}/speech-to-text/api/v1/recognize" \
        + "?model=en-US_BroadbandModel").format('us-east')

def get_auth():
    """get authentications for transcription service"""
    # print('AUTH')
    # print(os.environ.get('IBM_SPEECH_TO_TEXT_APIKEY'))
    apikey = str(os.environ.get('IBM_SPEECH_TO_TEXT_APIKEY'))
    return ("apikey", apikey)

def get_headers():
    """get authentication headers for transcription service"""
    headers = {}
    userpass = ":".join(get_auth())
    headers["Authorization"] = "Basic " + base64.b64encode(
        userpass.encode()).decode()
    return headers

def get_init_params():
    """ get params to to initialise Watson API"""
    return {
        "word_confidence": False,
        "content_type": "audio/l16;rate=16000;channels=1",
        "action": "start",
        "interim_results": False,
        "speech_detector_sensitivity": 0.5,
        "background_audio_suppression": 0.5,
    }

class IbmAsrService(MqttService):
    """
    This class listens for mqtt audio packets and publishes asr/text messages

    It integrates silence detection to slice up audio and detect the end of a spoken message
    It is designed to be run as a thread by calling run(run_event) (implemented in MqttService)

    To activate the service for a site send a message - hermod/<site>/asr/activate
    Once activated, the service will start listening for audio packets when you send
    - hermod/<site>/asr/start
    The service will continue to listen and emit hermod/<site>/asr/text messages every time the
    deepspeech engine can recognise some non empty text
    A hermod/<site>/asr/stop message will disable recognition while still leaving a loaded
     deepspeech transcription instance for the site so it can be reenabled instantly
    A hermod/<site>/asr/deactivate message will garbage collect any resources related to the site.
    """
    FORMAT = pyaudio.paInt16
    # Network/VAD rate-space
    RATE_PROCESS = 16000
    CHANNELS = 1
    BLOCKS_PER_SECOND = 50

    def __init__(
            self,
            config,
            loop
        ):
        """constructor"""
        self.config = config
        self.loop = loop

        super(IbmAsrService, self).__init__(config, loop)
        # self.thread_targets.append(self.startASR)

        self.sample_rate = self.RATE_PROCESS
        self.block_size = int(self.RATE_PROCESS /float(self.BLOCKS_PER_SECOND))
        self.frame_duration_ms = 1000 * self.block_size // self.sample_rate
        self.vad = webrtcvad.Vad(config['services']['IbmAsrService'].get('vad_sensitivity', 1))

        self.last_start_id = {}
        self.audio_stream = {}  # BytesLoop()
        self.started = {}  # False
        self.active = {}  # False
        self.models = {}
        self.empty_count = {}
        self.restart_count = {}
        self.stream_contexts = {}
        self.ring_buffer = {}
        self.last_audio = {}
        self.ibmlistening = {}
        self.connections = {}
        self.no_packet_timeouts = {}
        self.total_time_timeouts = {}
        self.last_dialog_id = {}

        self.subscribe_to = 'hermod/+/asr/activate,hermod/+/asr/deactivate,hermod/+/asr/start' \
        + ',hermod/+/asr/stop,hermod/+/hotword/detected'
        self.audio_count = 0
        # this_folder = os.path.dirname(os.path.realpath(__file__))
        # wav_file = os.path.join(this_folder, 'turn_off.wav')
        # f = open(wav_file, "rb")
        # self.turn_off_wav = f.read();
        # self.eventloop = asyncio.new_event_loop()
        # asyncio.set_event_loop(self.eventloop)
        # self.log('START ibm ASR')
        # self.log(this_folder)

        # self.startASR()

   


    async def on_message(self, message):
        """handle mqtt message"""
        topic = "{}".format(message.topic)
        # self.log("ASR MESSAGE {}".format(topic))
        parts = topic.split("/")
        site = parts[1]
        if topic == 'hermod/' + site + '/asr/activate':
            self.log('activate ASR ' + site)
            await self.activate(site)
        elif topic == 'hermod/' + site + '/asr/deactivate':
            self.log('deactivate ASR ' + site)
            await self.deactivate(site)
        elif topic == 'hermod/' + site + '/asr/start':
            # self.log('start ASR '+site)
            if not self.active.get(site, False):
                await self.activate(site)
            self.log('start ASR ' + site)
            # timeout if no packets
            if site in self.no_packet_timeouts:
                self.no_packet_timeouts[site].cancel()
            self.no_packet_timeouts[site] = self.loop.create_task(
                self.no_packet_timeout(site))
            # total time since start
            if site in self.total_time_timeouts:
                self.total_time_timeouts[site].cancel()
            self.total_time_timeouts[site] = self.loop.create_task(
                self.total_time_timeout(site))
            payload = {}
            payload_text = message.payload
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                pass
            self.last_dialog_id[site] = payload.get('id', '')
            self.started[site] = True
            self.last_audio[site] = time.time()
            payload = {}
            try:
                payload = json.loads(message.payload)
            except json.JSONDecodeError:
                pass
            self.last_start_id[site] = payload.get('id', '')
            self.loop.create_task(self.start_asr_vad(site))
            # await self.startASR(site)
        elif topic == 'hermod/' + site + '/asr/stop':
            self.log('stop ASR ' + site)
            # clear timeouts
            if site in self.no_packet_timeouts:
                self.no_packet_timeouts[site].cancel()
            # total time since start
            if site in self.total_time_timeouts:
                self.total_time_timeouts[site].cancel()
            # should be   finish_stream ?
            if site in self.connections:
                try:
                    await self.connections[site].close()
                except Exception:
                    pass
            self.started[site] = False
            # self.client.publish('hermod/'+site+'/speaker/play',self.turn_off_wav)

        elif topic == 'hermod/' + site + '/hotword/detected':
            self.log('clear buffer ' + site)
            if site in self.ring_buffer:
                self.ring_buffer[site].clear()
            # self.client.publish('hermod/'+site+'/speaker/play',self.turn_off_wav)

        elif topic == 'hermod/' + site + '/microphone/audio':
            if self.started.get(site, False):
                self.audio_count = self.audio_count + 1
                self.audio_stream[site].write(message)

    async def activate(self, site):
        """activate asr service"""
        self.audio_stream[site] = BytesLoop()
        self.active[site] = True
        self.started[site] = False
        await self.client.subscribe('hermod/' + site + '/microphone/audio')

    async def deactivate(self, site):
        """deactivate asr service"""
        await self.client.unsubscribe('hermod/' + site + '/microphone/audio')
        self.audio_stream.pop(site, '')
        self.active[site] = False
        self.started[site] = False

    async def total_time_timeout(self, site):
        """total timeout callback"""
        await asyncio.sleep(12)
        if site in self.no_packet_timeouts:
            self.no_packet_timeouts[site].cancel()
        await self.finish_stream(site)

    async def no_packet_timeout(self, site):
        """no packets timeout callback"""
        await asyncio.sleep(3.5)
        print('SILENCE TIMEOUT')
        if site in self.total_time_timeouts:
            self.total_time_timeouts[site].cancel()
        await self.finish_stream(site)

    async def timeout(self, site, conn):
        """send timeout messages"""
        await self.client.publish('hermod/' + site + '/asr/timeout', json.dumps({
            "id": self.last_start_id.get(site, '')
        }))
        await self.client.publish('hermod/' + site + '/dialog/end', json.dumps({
            "id": self.last_start_id.get(site, '')
        }))
        self.started[site] = False
        await conn.close()

    async def finish_stream(self, site):
        """finish transcription stream"""
        try:
            self.ibmlistening[site] = False
            if site in self.connections:
                self.log('FINISH STREAM send stop')
                await self.connections[site].send(json.dumps({'action': 'stop'}))
                # self.started[site] = False
            else:
                self.log('FINISH STREAM no connection')
                self.started[site] = False
        except Exception:
            self.log('FINISH STREAM error')
            # self.log(type(e))
            # self.log(e)
            self.started[site] = False
            # pass

    async def start_asr_vad(self, site=''):
        """start transcription stream"""
        self.log('ASRVAD start')
        # await self.send_sound('on',site)
        # await self.client.publish('hermod/'+site+'/speaker/play',json.dumps({"sound":"on"}))
        # return
        text = ''
        sender = None
        # reconnect on error while started and no text heard
        self.empty_count[site] = 0
        # while site in self.started and self.started[site] \
        # and not len(text) > 0 and self.empty_count[site] < 4:
        # self.empty_count[site] = 0
        # clear stream buffer
        self.audio_stream[site] = BytesLoop()
        # NEW
        self.log('ASRVAD CONNECT')
        async with websockets.connect(get_url(), extra_headers=get_headers()) as conn:
            # CONFIGURE SOCKET SESSION
            self.connections[site] = conn
            await conn.send(json.dumps(get_init_params()))
            await conn.recv()
            # print(rec)
            self.ibmlistening[site] = True
            # SEND AUDIO PACKETS
            # clear task from previous loop
            if sender:
                self.log('AUDIO SENDER CLEAR ')
                sender.cancel()
            sender = asyncio.create_task(self.send_audio(conn, site))
            # self.log('ASRVAD start sound')
            # self.log('ASRVAD start sound DONE')
            # Keeps receiving transcript until we have the final transcript
            while True:
                self.log('ASRVAD LOOP')

                # if self.empty_count[site] >= 4:
                    # await
                    # self.client.publish('hermod/'+site+'/aser/timeout',json.dumps({
                        # "id": self.last_start_id.get(site, '')
                    # }))
                    # await self.client.publish('hermod/'+site+'/dialog/end',json.dumps({
                        # "id":self.last_start_id.get(site,'')
                    # }))
                    # self.started[site] = False
                    # break
                try:
                    rec = await conn.recv()
                    parsed = json.loads(rec)
                    print('=============================')
                    print(parsed)
                    print('=============================')

                    if parsed.get("error", False):
                        self.log('ASRVAD ERROR FROM IBM')
                        self.log(parsed.get('error'))
                        # self.empty_count[site] = self.empty_count[site]  + 1
                        # self.ibmlistening[site] = False
                        # try:
                            # #await self.client.publish('hermod/'+site+'/dialog/end',
                            # json.dumps({"id":self.last_start_id.get(site,'')}))
                            # await conn.close()
                        # except Exception:
                            # pass
                        await self.timeout(site, conn)
                        break

                    if parsed.get('state', False) and parsed.get('state') == 'listening':
                        self.log('ASRVAD SET LISTENING '+site)
                        self.ibmlistening[site] = True

                    # have_results = False
                    if "results" in parsed:
                        self.log('RESULTS')
                        self.log(parsed["results"])
                        if parsed["results"]:
                            if "final" in parsed["results"][0]:
                                if parsed["results"][0]["final"]:
                                    if parsed["results"][0]['alternatives']:
                                        text = str(parsed["results"][0]["alternatives"][0].get(\
                                        "transcript", ""))
                                        self.log('ASRVAD got text [{}]'.format(text))
                                        if text:
                                            # self.log('send content '+site)
                                            # self.log(self.client)
                                            # self.log('hermod/'+site+'/asr/text')
                                            # self.log(json.dumps({'text':text}))
                                            # have_results = True
                                            self.empty_count[site] = 0
                                            await self.client.publish('hermod/'+site+'/asr/text', \
                                            json.dumps({
                                                'text':text,
                                                "id":self.last_start_id.get(site, '')
                                            }))
                                            # self.log('sent content '+text)
                                            self.started[site] = False
                                            await conn.close()
                                            break
                        else:
                            if self.empty_count[site] < 3:
                                self.empty_count[site] = self.empty_count[site] + 1
                            else:
                                self.timeout(site)
                            # await self.timeout(site,conn)
                            # break
                        # if not have_results:
                            # self.log('ASRVAD incc emtpy f'+ str(self.empty_count[site]))
                            # self.empty_count[site] = self.empty_count[site]  + 1
                            # self.ibmlistening[site] = False

                                        # conn.close()
                                        # return False
                                        # pass
                except KeyError:
                    await self.timeout(site, conn)
                    break
                except Exception:
                    await self.timeout(site, conn)
                    break

        # cleanup
        self.started[site] = False
        self.ibmlistening[site] = False
        if sender:
            sender.cancel()
        try:
            await conn.close()
        except Exception:
            pass



    async def send_audio(self, websocket_service, site):
        """send audio to transcription service"""
        # Starts recording of microphone
        print("AUDIOSENDER * READY *"+site)
        have_frame = False
        async for frame in self.vad_collector(site):
            # self.log('AUDIOLOOP frame {} {}'.format(site,self.empty_count[site]))
            # if self.empty_count[site] > 2 and self.started[site]:
                # self.log('AUDIOSENDER END    TIMEOUT EMPTY')
                # await self.client.publish('hermod/'+site+'/asr/timeout',json.dumps({
                    # "id":self.last_start_id.get(site,'')
                # }))
                # await self.client.publish('hermod/'+site+'/dialog/end',json.dumps({
                    # "id":self.last_start_id.get(site,'')
                # }))
                # self.started[site] = False
                # break

            if self.started[site] and self.ibmlistening.get(site, False):
                # self.log('is started '+site)
                # self.started[site] == False
                if frame is not None:
                    # self.log('feed content '+site)
                    # self.log(self.models)
                    # self.log(self.stream_contexts)
                    try:
                        print(len(frame))
                        # data = stream.read(CHUNK)
                        if len(frame) > 100:
                            await websocket_service.send(frame) #np.frombuffer(frame, np.int16))
                            if site in self.no_packet_timeouts:
                                self.no_packet_timeouts[site].cancel()
                            self.no_packet_timeouts[site] = self.loop.create_task(\
                            self.no_packet_timeout(site))
                        else:
                            self.log('skip tiny frame')
                        have_frame = True
                        # self.stream_contexts[site].feedAudioContent
                        #(np.frombuffer(frame, np.int16))
                        # self.log('fed content')
                    except Exception:
                        self.log('AUDIOSENDER error feeding content')
                        break
                # ignore None from vad_collector if it's the first
                elif have_frame:
                    print('AUDIOSENDER END     NOFRAME  -END BY VAD COLLECTOR')
                    # text = self.stream_contexts[site].finishStream()
                    await self.finish_stream(site)
                    # break


    # coroutine
    async def frame_generator(self, site):
        """Generator that yields all audio frames."""
        # silence_count = 0;
        while True and self.started.get(site, False):
            # if silence_count == 30:
                # self.log('FRAMEGEN no voice packets timeout')
                # # await self.client.publish('hermod/'+site+'/timeout',json.dumps({}))
                # # await self.client.publish('hermod/'+site+'/dialog/end', \
                #json.dumps({"id":self.last_start_id.get(site,'')}))
                # #await self.finish_stream(site)
                # #self.started[site] = False
                # #break

            if site in self.audio_stream and self.audio_stream[site].has_bytes(self.block_size*2) \
            and site in self.ibmlistening and self.ibmlistening.get(site):
                # self.log('have audiuo rame')
                # silence_count = 0;
                yield self.audio_stream[site].read(self.block_size*2)
            else:
                # hand off control to other frame generators without yielding a value
                # self.log('NO have audiuo rame '+str(silence_count))
                # silence_count = silence_count + 1;
                await asyncio.sleep(0.01)
            # padding_ms=300

    async def vad_collector(self, site, padding_ms=280, ratio=0.75, frames=None):
        """Generator that yields series of consecutive audio frames comprising each utterence,
         separated by yielding a single None.
        Determines voice activity by ratio of frames in padding_ms. Uses a buffer to include
        padding_ms prior to being triggered.
            Example: (frame, ..., frame, None, frame, ..., frame, None, ...)
                      |---utterence---|        |---utterence---|
        """
        # if frames is None: frames =
        num_padding_frames = padding_ms // self.frame_duration_ms
        self.ring_buffer[site] = collections.deque(maxlen=num_padding_frames)
        triggered = False
        self.last_audio[site] = time.time()
        # last_audio = time.time()
        async for frame in self.frame_generator(site):
            # now = time.time()
            # self.log('VADLOOP')
            # self.log(now - last_audio)
            # if (now -  self.last_audio[site]) > 10 and self.active[site] == True and \
            #self.started[site]:
                # self.log('ASR silence TIMEOUT')
                # await self.client.publish('hermod/'+site+'/timeout',json.dumps({}))
                # await self.client.publish('hermod/'+site+'/dialog/end',json.dumps({
                    # "id":self.last_start_id.get(site,'')
                # }))
                # break;

            if len(frame) < 1:  # 640
                self.log('ibm short frame')
                yield None
                # return

            is_speech = self.vad.is_speech(frame, self.sample_rate)
            # self.log('is speech {}'.format(is_speech))
            if not triggered:
                # self.log('not triggered')
                self.ring_buffer[site].append((frame, is_speech))
                num_voiced = len([fchunk for fchunk, speech in self.ring_buffer[site] if speech])
                if num_voiced > ratio * self.ring_buffer[site].maxlen:
                    # self.log('push trigger')

                    triggered = True
                    for chunk in self.ring_buffer[site]:
                        yield chunk
                    self.ring_buffer[site].clear()

            else:
                # self.log(' triggered')
                self.last_audio[site] = time.time()
                yield frame
                self.ring_buffer[site].append((frame, is_speech))
                num_unvoiced = len([fchunk for fchunk, speech in self.ring_buffer[site] \
                if not speech])
                if num_unvoiced > ratio * self.ring_buffer[site].maxlen:
                    # self.log(' untriggered')
                    triggered = False
                    yield None
                    self.ring_buffer[site].clear()
