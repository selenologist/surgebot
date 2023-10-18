#!/usr/bin/env python3

import surgepy

import discord
import soundfile

import numpy as np

import io
import asyncio
import tempfile
import math
import os
import sys

from concurrent.futures import ProcessPoolExecutor

## tuneables

SAMPLE_RATE = 48000
ROOT_NOTE = 33 # A1
OCTAVES = 5
SECONDS_ON = 1 # seconds of note on
SECONDS_OFF = 0.5 # seconds of note off

# n.b. token is passed via environment variable

## globals

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

surge_version_string = "||Version: " + surgepy.getVersion() + "||"

# we really just want to not stall the network task with our cpu,
# but ok let's assume we have a 4-thread cpu
pool = ProcessPoolExecutor(max_workers=3)

## program

# called in a separate process, does a whole new surge each time
def surge_patch_to_flac(label, patch_path):
    s = surgepy.createSurge(SAMPLE_RATE)

    if SAMPLE_RATE != s.getSampleRate():
        print("requested sample rate", SAMPLE_RATE, " but got ", s.getSampleRate(), " instead.")

    # now don't touch the global again

    s.loadPatch(patch_path)

    buf = s.createMultiBlock(math.ceil((OCTAVES * (SECONDS_ON + SECONDS_OFF) + SECONDS_OFF * 8) * s.getSampleRate() / s.getBlockSize()))

    pos = 0;
    hold = math.ceil(SECONDS_ON * s.getSampleRate() / s.getBlockSize())
    silence = math.ceil(SECONDS_OFF * s.getSampleRate() / s.getBlockSize())
   
    # settle for the silence interval
    s.processMultiBlock(buf, pos, silence)
    pos = pos + silence
   
    for i in range(OCTAVES):
        note = ROOT_NOTE + i * 12
        # Play note on channel 0 at velcity 127 with 0 detune
        s.playNote(0, note, 127, 0)
        s.processMultiBlock(buf, pos, hold)
        pos = pos + hold

        # and release the note
        s.releaseNote(0, note, 0)
        s.processMultiBlock(buf, pos, silence)
        pos = pos + silence
    
    # run for 7x the silence interval
    s.processMultiBlock(buf, pos, silence*7)
    pos = pos + silence # (unnecessary)

    flac_file = io.BytesIO()
    soundfile.write(flac_file, np.transpose(buf), int(s.getSampleRate()), subtype='PCM_16', format='FLAC')

    return label, flac_file.getvalue()


@client.event
async def on_ready():
    print(f'We have logged in as {client.user}')

@client.event
async def on_message(message):
    # ignore our own messages
    if message.author == client.user:
        return

    if "surgebot must die" in message.content:
        await client.close()
        sys.exit(0)

    fxp_attachments = []
    for attachment in message.attachments:
        if attachment.filename.endswith(".fxp"):
            fxp_attachments.append(attachment)

    if fxp_attachments:
        jump_url = message.jump_url

        filenames = ", ".join([a.filename for a in fxp_attachments])
        message = await message.channel.send('Generating recording for ['+filenames+'], please wait.', reference=message)

        fxp_files = []
        fxp_files_fut = []
        for attachment in fxp_attachments:
            tmp = tempfile.NamedTemporaryFile()
            fxp_files_fut.append(attachment.save(tmp.name))
            fxp_files.append([attachment.filename.removesuffix(".fxp")+".flac", tmp])
        fxp_files_fut = await asyncio.gather(*fxp_files_fut)

        #message, _fxp_files_fut = await asyncio.gather(message, fxp_files_fut)
   
        flac_futs = [pool.submit(surge_patch_to_flac, f[0], f[1].name) for f in fxp_files]
        flac_files = [await asyncio.wrap_future(fut) for fut in flac_futs]
        
        await message.edit(content=surge_version_string,
                           attachments=[discord.File(io.BytesIO(flac), filename=label) for label, flac in flac_files])

# get token from environment variable
client.run(os.environ["SURGEBOT_DISCORD_TOKEN"])
