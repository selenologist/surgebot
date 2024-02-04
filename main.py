#!/usr/bin/env python3

import surgepy

import discord
import soundfile
import mido

import numpy as np

import io
import asyncio
import tempfile
import math
import os
import sys
import glob

from concurrent.futures import ProcessPoolExecutor

## tuneables

MESSAGE_DELETION_EMOJI = '🗑️'
MESSAGE_DELETION_EMOJI_TIME = 60

# Simple octaves default note generator configuration

SAMPLE_RATE = 48000
ROOT_NOTE = 33 # A1
OCTAVES = 5
SECONDS_ON = 1 # seconds of note on
SECONDS_OFF = 0.5 # seconds of note off

# MIDI note generator configuration
MAX_TIME = 30 # seconds

# n.b. token is passed via environment variable

## globals

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

surge_version_string = "||Version: " + surgepy.getVersion() + "||"

# we really just want to not stall the network task with our cpu-bound rendering
pool = ProcessPoolExecutor(max_workers=2)

midi_commands = {}
def populate_midi_commands():
    global midi_commands
    midi_commands = {}
    for midi in glob.glob("midis/*.mid"):
        command = "!" + midi.removeprefix("midis/").removesuffix(".mid").lower().replace(' ', '_')
        midi_commands[command] = midi
populate_midi_commands()

### program

## this section is called in a separate process, and creates a whole new surge each time

# default patch demo generator when no MIDI was specified
def default_octaves_note_generator(s):
    buf = s.createMultiBlock(math.ceil((OCTAVES * (SECONDS_ON + SECONDS_OFF) + SECONDS_OFF * 8) * s.getSampleRate() / s.getBlockSize()))

    pos = 0;
    hold = math.ceil(SECONDS_ON * s.getSampleRate() / s.getBlockSize())
    silence = math.ceil(SECONDS_OFF * s.getSampleRate() / s.getBlockSize())

    # settle for the silence interval
    s.processMultiBlock(buf, pos, silence)
    pos = pos + silence

    for i in range(OCTAVES):
        note = ROOT_NOTE + i * 12
        # Play note on channel 0 at velocity 127 with 0 detune
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

    return buf

def midi_note_generator(s, midi_path, mpe):
    midi_file = mido.MidiFile(midi_path)

    blocks_per_second = s.getSampleRate() / s.getBlockSize()
    block_count = math.ceil(blocks_per_second * MAX_TIME)
    buf = s.createMultiBlock(block_count)

    time = 0
    block = 0

    for msg in midi_file:
        if time + msg.time > MAX_TIME:
            break
        if msg.time != 0:
            blocks = math.floor(msg.time * blocks_per_second)
            if blocks > 0: # fold messages smaller than a block increment into each other. bad?
                s.processMultiBlock(buf, block, blocks)
                block += blocks
                time += msg.time
        if msg.type == 'note_on' and msg.velocity != 0:
            s.playNote(msg.channel if mpe else 0, msg.note, msg.velocity, 0)
        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
            s.releaseNote(msg.channel if mpe else 0, msg.note, msg.velocity)
        elif msg.type == 'pitchwheel':
            s.pitchBend(msg.channel if mpe else 0, msg.pitch)
        elif msg.type == 'aftertouch':
            s.channelAftertouch(msg.channel if mpe else 0, msg.value)
        elif msg.type == 'polytouch':
            s.polyAftertouch(msg.channel if mpe else 0, msg.note, msg.value)
        elif msg.type == 'control_change':
            s.channelController(msg.channel if mpe else 0, msg.control, msg.value)

    if block < block_count:
        s.processMultiBlock(buf, block, block_count - block)

    return buf

# entry point of separate process
def surge_patch_to_flac(label, patch_path, midi_path, mpe):
    s = surgepy.createSurge(SAMPLE_RATE)

    s.mpeEnabled = mpe

    if SAMPLE_RATE != s.getSampleRate():
        print("requested sample rate", SAMPLE_RATE, " but got ", s.getSampleRate(), " instead.")

    # now don't touch the global again

    s.loadPatch(patch_path)

    buf = None
    if midi_path:
        buf = midi_note_generator(s, midi_path, mpe)
    else:
        buf = default_octaves_note_generator(s)

    # normalize and transpose buffer
    abs_max = max(abs(buf.min()), abs(buf.max()))
    buf = np.transpose(buf / (abs_max * 1.5))

    flac_file = io.BytesIO()
    soundfile.write(flac_file, buf, int(s.getSampleRate()), subtype='PCM_16', format='FLAC')

    return label, flac_file.getvalue()

## the rest executes in the main process

@client.event
async def on_ready():
    print(f'We have logged in as {client.user}')

@client.event
async def on_reaction_add(reaction, user):
    if reaction.me:
        return

    if reaction.emoji == MESSAGE_DELETION_EMOJI and reaction.message.author == client.user:
        await reaction.message.delete()

pending_removal = set()
async def remove_deletion_emoji_later(message):
    await asyncio.sleep(MESSAGE_DELETION_EMOJI_TIME)
    await message.remove_reaction(MESSAGE_DELETION_EMOJI, client.user)
def queue_remove_deletion_emoji_later(message):
    task = client.loop.create_task(remove_deletion_emoji_later(message))
    pending_removal.add(task)
    task.add_done_callback(pending_removal.discard)

@client.event
async def on_message(message):
    # ignore our own messages
    if message.author == client.user or message.content.startswith("!no"):
        return

    if message.content.startswith("!surgebot midi"):
        if "repop" in message.content:
            populate_midi_commands()
        await message.channel.send(", ".join(sorted(midi_commands.keys())), reference=message)

    audio_attachments = False
    fxp_attachments = []
    for attachment in message.attachments:
        if attachment.filename.endswith(".fxp"):
            fxp_attachments.append(attachment)
        if attachment.filename.endswith(".ogg")  or \
           attachment.filename.endswith(".mp3")  or \
           attachment.filename.endswith(".wav")  or \
           attachment.filename.endswith(".flac") or \
           attachment.filename.endswith(".opus"):
            audio_attachments = True

    if fxp_attachments:
        fxp_attachments = fxp_attachments[:4] # limit to the first 4 attachments

        jump_url = message.jump_url
        midi_path = None # never set directly from user input, comes from `midi_commands`

        mpe_enabled = "!mpe" in message.content

        # check first block delimited by whitespace for a midi command
        first_word = message.content.split(maxsplit=1)[0] if message.content else None
        if first_word in midi_commands:
            midi_path = midi_commands[first_word]

        if audio_attachments and not midi_path:
            return # skip processing when the user already supplied a file

        filenames = ", ".join([a.filename for a in fxp_attachments])
        message = await message.channel.send('Processing ['+filenames+'], please wait.',
                                             reference = message, mention_author = False)

        await message.add_reaction(MESSAGE_DELETION_EMOJI)
        queue_remove_deletion_emoji_later(message)

        try:
            # download all FXPs from message concurrently, but wait for all of them to finish before proceeding
            fxp_files = []
            fxp_files_fut = []
            for attachment in fxp_attachments:
                tmp = tempfile.NamedTemporaryFile()
                fxp_files_fut.append(attachment.save(tmp.name))
                fxp_files.append([attachment.filename.removesuffix(".fxp")+".flac", tmp])
            fxp_files_fut = await asyncio.gather(*fxp_files_fut)

            # submit patches to worker processes for rendering
            flac_futs = [pool.submit(surge_patch_to_flac, f[0], f[1].name, midi_path, mpe_enabled) for f in fxp_files]
            # wait for all patches to finish rendering
            flac_files = [await asyncio.wrap_future(fut) for fut in flac_futs]

            await message.edit(content=surge_version_string,
                               attachments=[discord.File(io.BytesIO(flac), filename=label) for label, flac in flac_files])
        except Exception as e:
            await message.edit(content="Sorry, an error occurred. Please try again later.\nDetails:\n```python\n{}\n```".format(e))

# get token from environment variable
client.run(os.environ["SURGEBOT_DISCORD_TOKEN"])
