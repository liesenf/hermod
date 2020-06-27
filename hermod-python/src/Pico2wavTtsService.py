"""
This class listens for tts/say messages and triggers a sequence of messages
that result in the text message being converted to wav audio and played through the speaker service
TODO Where the text is very long, it is split into parts and sent sequentially.
The speaker service sends start and end messages.
This service iterates each part, waiting for each speaker/started and speaker/finished message
and finally sends a tts/finished  message when all parts have finished playing
Depends on os pico2wav install with path in config.yaml
"""

import json
import os
import aiofiles
import concurrent.futures
import asyncio
from random import seed
from random import randint
from MqttService import MqttService
import unicodedata
import string
from pathlib import Path
        
valid_filename_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
char_limit = 240


# seed random number generator
seed(1)

def os_system(command):
    os.system(command)

def clean_filename(filename, whitelist=valid_filename_chars, replace=' '):
    # replace spaces
    for r in replace:
        filename = filename.replace(r,'_')
    
    # keep only valid ascii chars
    cleaned_filename = unicodedata.normalize('NFKD', filename).encode('ASCII', 'ignore').decode()
    
    # keep only whitelisted chars
    cleaned_filename = ''.join(c for c in cleaned_filename if c in whitelist)
    if len(cleaned_filename)>char_limit:
        print("Warning, filename truncated because it was over {}. Filenames may no longer be unique".format(char_limit))
    return cleaned_filename[:char_limit]    
 

class Pico2wavTtsService(MqttService):
    """ Text to Speech Service Class """

    def __init__(
            self,
            config,
            loop
    ):
        super(
            Pico2wavTtsService,
            self).__init__(config,loop)
        self.config = config
        self.clients = {}
        # subscribe to all sites
        self.subscribe_to = 'hermod/+/tts/say,hermod/+/dialog/init'
        cache_path = self.config['services']['Pico2wavTtsService'].get('cache_path','/tmp/tts_cache')
        Path(cache_path).mkdir(parents=True, exist_ok=True)


    async def on_message(self, msg):
        topic = "{}".format(msg.topic)
        parts = topic.split('/')
        site = parts[1]
        payload = {}
        try:
            payload = json.loads(msg.payload)
        except BaseException:
            pass
        #self.log('message {} {}'.format(site,topic))
        #self.log(payload)
        text = payload.get('text')
        #self.log(text)
        if topic == 'hermod/' + site + '/tts/say':
            await self.generate_audio(site, text, payload)
        elif topic == 'hermod/' + site + '/speaker/finished':
            self.log('SPEAKER FINISHED')
            self.log(payload)
            #self.play_requests[payload.get('id')] = value;
          
            message = {"id": payload.get('id')}
            await asyncio.sleep(0.5)
            await self.client.publish(
                'hermod/{}/tts/finished'.format(site),
                json.dumps(message))
            await self.client.unsubscribe('hermod/{}/speaker/finished'.format(site))
        elif topic == 'hermod/' + site + '/dialog/init':
            self.log('PICO TTS CLIENT INIT')
            self.log(payload)
            self.log(site)
            self.clients[site] = payload

    async def cleanup_file(self,short_text,file_name):
        await asyncio.sleep(1)
         # cache short texts
        if len(short_text) > self.config.get('cache_max_letters',100):
             os.remove(file_name)
        self.log('CLEANUP TTS '+file_name)

    """ Use system binary pico2wav to generate audio file from text then send audio as mqtt"""
    async def generate_audio(self, site, text, payload):
        cache_path = self.config['services']['Pico2wavTtsService'].get('cache_path','/tmp/tts_cache')
        value = payload.get('id','no_id')
        
        if len(text) > 0:
            short_text = text[0:100].replace(' ','_').replace(".","")
            # speakable and limited
            say_text = text[0:300].replace('(','').replace(')','')
            short_file_name = clean_filename('tts-' + str(short_text)) + '.wav'
            file_name = os.path.join(cache_path, short_file_name)
            
            # short_text = text[0:100].replace(' ','_')
            # short_file_name =clean_filename('tts-' + str(short_text) + '.wav')
            # file_name = os.path.join(cache_path, short_file_name)
            
            # generate if file doesn't exist in cache
            if not os.path.isfile(file_name):
                path = self.config['services']['Pico2wavTtsService']['binary_path']
                command = path + ' -w=' + file_name + ' "{}" '.format(say_text)
                executor = concurrent.futures.ProcessPoolExecutor(
                    max_workers=1,
                )
                await self.loop.run_in_executor(executor,os_system,command)

            async with aiofiles.open(file_name, mode='rb') as f:
                audio_file = await f.read()
                await self.client.subscribe('hermod/{}/speaker/finished'.format(site))
                self.log(self.clients)
                if site in self.clients and self.clients[site].get('platform','') == "web"  and self.clients[site].get('url',False) :
                    self.log('SEND TTS AS URL'+self.clients[site].get('url')+"/"+short_file_name)
                    await self.client.publish(
                        'hermod/{}/speaker/play/{}'.format(site, value), payload=json.dumps({"url":self.clients[site].get('url')+"/tts/"+short_file_name}), qos=0)
                else:
                    self.log('SEND TTS AS packets')
                    slice_length = 2048
                    def chunker(seq, size):
                        return (seq[pos:pos + size] for pos in range(0, len(seq), size))
                    for slice in chunker(audio_file, slice_length):
                        await self.client.publish('hermod/{}/speaker/cache/{}'.format(site, value), payload=bytes(slice), qos=0)
                    
                    # finally send play message with empty payload
                    await self.client.publish(
                        'hermod/{}/speaker/play/{}'.format(site, value), payload=None, qos=0)
                
                await self.cleanup_file(short_text,file_name)
             
