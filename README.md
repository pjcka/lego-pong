# Lego Pong

A classic Pong game controlled by Lego Spike Prime hub motors!

## Hardware Requirements

- Lego Spike Prime (or Spark) Hub
- 2 motors connected to ports A and B
- USB connection to your Mac

## Controls

- **Motors**: Turn to move paddles (Player 1 = Port A, Player 2 = Port B)
- **Hub Button / Space**: Launch ball
- **D**: Toggle debug info
- **ESC**: Quit

## Features

- Skill level selection (1-5) affects paddle size and ball speed
- Ball speeds up every 4 hits
- Acceleration-based paddle control (small movements = precise, fast movements = quick)
- Fullscreen gameplay

## Installation

### Option 1: Run from source
```bash
pip3 install pygame pyserial
python3 pong.py
```

### Option 2: Use the macOS app
The `Lego Pong.app` bundle auto-installs dependencies on first run. Just double-click to play!

## Requirements

- Python 3
- macOS (for the .app bundle)
- Lego Spike Prime hub with USB connection
