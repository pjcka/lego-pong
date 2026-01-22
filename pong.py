#!/usr/bin/env python3
"""
Lego Pong - A Pong game controlled by Lego Spark hub motors.
Motors on ports A and E act as rotation controllers for the paddles.
"""

import pygame
import serial
import serial.tools.list_ports
import threading
import time
import sys
import re
import glob

BAUD_RATE = 115200


def find_hub_port():
    """Auto-detect the Lego hub serial port."""
    # Look for USB modem devices (typical for Lego hubs)
    patterns = ["/dev/cu.usbmodem*", "/dev/tty.usbmodem*"]

    for pattern in patterns:
        ports = glob.glob(pattern)
        if ports:
            return ports[0]

    # Fallback: use pyserial's port detection
    for port in serial.tools.list_ports.comports():
        if "usbmodem" in port.device.lower() or "lego" in port.description.lower():
            return port.device

    return None

# Game constants
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600
PADDLE_WIDTH = 15
PADDLE_HEIGHT = 100
BALL_SIZE = 15
PADDLE_MARGIN = 30
FPS = 60

# Colors
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)

# Motor positions (shared between threads)
motor_a_position = 0
motor_b_position = 0
motor_a_delta = 0
motor_b_delta = 0
last_motor_a = None
last_motor_b = None
hub_button_pressed = False
position_lock = threading.Lock()
hub_connected = False


def init_hub(ser):
    """Initialize hub and set motors to position mode."""
    ser.reset_input_buffer()
    ser.write(b'\x03')  # Ctrl+C to get to REPL
    time.sleep(0.3)
    ser.reset_input_buffer()
    ser.write(b'import hub\r\n')
    time.sleep(0.1)
    # Set motors to position mode (mode 2 = POS, cumulative degrees)
    ser.write(b'hub.port.A.motor.mode([(2,0)])\r\n')
    time.sleep(0.1)
    ser.write(b'hub.port.B.motor.mode([(2,0)])\r\n')
    time.sleep(0.1)
    ser.reset_input_buffer()


def read_motor_positions(ser):
    """Read motor positions and calculate deltas."""
    global motor_a_position, motor_b_position, hub_connected
    global motor_a_delta, motor_b_delta, last_motor_a, last_motor_b
    global hub_button_pressed

    try:
        # Send command to read motor positions and button state
        ser.reset_input_buffer()
        cmd = b'print("POS:", hub.port.A.motor.get()[0], hub.port.B.motor.get()[0], hub.button.center.is_pressed())\r\n'
        ser.write(cmd)
        time.sleep(0.05)

        # Read response
        response = ser.read(ser.in_waiting or 200).decode('utf-8', errors='ignore')

        # Parse the position values and button state
        match = re.search(r'POS:\s*(-?\d+)\s+(-?\d+)\s+(True|False)', response)
        if match:
            new_a = int(match.group(1))
            new_e = int(match.group(2))
            button = match.group(3) == 'True'

            with position_lock:
                # Calculate deltas (change since last reading)
                if last_motor_a is not None:
                    motor_a_delta += new_a - last_motor_a
                if last_motor_b is not None:
                    motor_b_delta += new_e - last_motor_b

                last_motor_a = new_a
                last_motor_b = new_e
                motor_a_position = new_a
                motor_b_position = new_e
                hub_button_pressed = button
                hub_connected = True
            return True
    except Exception as e:
        pass

    return False


def hub_communication_thread(stop_event):
    """Background thread for hub communication."""
    global hub_connected

    while not stop_event.is_set():
        port = find_hub_port()
        if not port:
            print("No Lego hub found. Waiting for connection...")
            time.sleep(2)
            continue

        try:
            ser = serial.Serial(port, BAUD_RATE, timeout=0.1)
            print(f"Connected to hub on {port}")
            init_hub(ser)
            print("Hub initialized")
            hub_connected = True

            while not stop_event.is_set():
                if not read_motor_positions(ser):
                    time.sleep(0.1)
                else:
                    time.sleep(0.02)  # ~50Hz update rate

            ser.close()
            break
        except serial.SerialException as e:
            print(f"Serial error: {e}")
            hub_connected = False
            time.sleep(2)  # Wait before retry


def motor_to_paddle_y(motor_pos, screen_height, paddle_height):
    """Convert motor position to paddle Y position."""
    # APOS mode gives -180 to 179 degrees
    # Map this to paddle Y position (0 = top, max = bottom)

    min_y = 0
    max_y = screen_height - paddle_height

    # Shift from (-180 to 179) to (0 to 359)
    normalized = (motor_pos + 180) % 360

    # Use 180 degrees of rotation for full travel (half turn)
    # Center point (0 degrees APOS = 180 normalized) = middle of screen
    # Map 90-270 normalized (i.e., -90 to +90 APOS) to full paddle range

    # Simpler: map -90 to +90 degrees to full paddle travel
    clamped = max(-90, min(90, motor_pos))

    # Map from (-90, 90) to (0, max_y)
    return int(((clamped + 90) / 180) * max_y)


class Paddle:
    def __init__(self, x, y, height=PADDLE_HEIGHT):
        self.height = height
        self.rect = pygame.Rect(x, y, PADDLE_WIDTH, height)

    def move_to(self, y):
        self.rect.y = max(0, min(SCREEN_HEIGHT - self.height, y))

    def draw(self, screen):
        pygame.draw.rect(screen, WHITE, self.rect)


class Ball:
    def __init__(self, p1_skill=3, p2_skill=3):
        self.rect = pygame.Rect(0, 0, BALL_SIZE, BALL_SIZE)
        self.p1_skill = p1_skill
        self.p2_skill = p2_skill
        self.base_speed = 7
        self.speed = self.base_speed
        self.hit_count = 0
        self.dx = 0
        self.dy = 0
        self.attached_to = None  # None, 1 (paddle1), or 2 (paddle2)
        self.attach_to_paddle(2)  # Start with player 2

    def get_skill_speed_multiplier(self, target_player):
        """Get speed multiplier based on target player's skill."""
        skill = self.p1_skill if target_player == 1 else self.p2_skill
        # Skill 1 = 0.6x speed, Skill 3 = 1.0x, Skill 5 = 1.4x
        return 0.6 + (skill - 1) * 0.2

    def attach_to_paddle(self, player):
        """Attach ball to a paddle (1 or 2)."""
        self.attached_to = player
        self.dx = 0
        self.dy = 0
        self.speed = self.base_speed
        self.hit_count = 0

    def launch(self):
        """Launch the ball from the paddle it's attached to."""
        if self.attached_to is not None:
            # Ball goes toward the other player
            target = 1 if self.attached_to == 2 else 2
            direction = -1 if self.attached_to == 2 else 1
            skill_mult = self.get_skill_speed_multiplier(target)
            self.dx = direction * self.speed * skill_mult
            self.dy = 3
            self.attached_to = None

    def update(self, paddle1, paddle2):
        """Update ball position and handle collisions. Returns scorer (1 or 2) or None."""
        # If attached, follow the paddle
        if self.attached_to == 1:
            self.rect.x = paddle1.rect.right + 5
            self.rect.centery = paddle1.rect.centery
            return None
        elif self.attached_to == 2:
            self.rect.x = paddle2.rect.left - BALL_SIZE - 5
            self.rect.centery = paddle2.rect.centery
            return None

        # Move ball
        self.rect.x += self.dx
        self.rect.y += self.dy

        # Top/bottom wall collision
        if self.rect.top <= 0 or self.rect.bottom >= SCREEN_HEIGHT:
            self.dy = -self.dy

        # Paddle collisions
        if self.rect.colliderect(paddle1.rect) and self.dx < 0:
            self.hit_count += 1
            # Speed up every 3 hits
            if self.hit_count % 3 == 0:
                self.speed = min(self.speed + 1, 20)  # Cap at 20

            # Ball now heading toward player 2
            skill_mult = self.get_skill_speed_multiplier(2)
            self.dx = self.speed * skill_mult

            # Angle based on hit position (edge = more angle & slightly faster)
            relative_hit = (self.rect.centery - paddle1.rect.centery) / (paddle1.height / 2)
            self.dy = relative_hit * (self.speed * 0.8)

        if self.rect.colliderect(paddle2.rect) and self.dx > 0:
            self.hit_count += 1
            # Speed up every 3 hits
            if self.hit_count % 3 == 0:
                self.speed = min(self.speed + 1, 20)  # Cap at 20

            # Ball now heading toward player 1
            skill_mult = self.get_skill_speed_multiplier(1)
            self.dx = -self.speed * skill_mult

            # Angle based on hit position
            relative_hit = (self.rect.centery - paddle2.rect.centery) / (paddle2.height / 2)
            self.dy = relative_hit * (self.speed * 0.8)

        # Scoring
        if self.rect.left <= 0:
            return 2  # Player 2 scores
        if self.rect.right >= SCREEN_WIDTH:
            return 1  # Player 1 scores

        return None

    def draw(self, screen):
        pygame.draw.rect(screen, WHITE, self.rect)


def get_paddle_height(skill):
    """Return paddle height based on skill level (1-5). Higher skill = smaller paddle."""
    heights = {
        1: 180,  # Beginner - large paddle
        2: 140,
        3: 100,  # Normal
        4: 70,
        5: 50,   # Expert - small paddle
    }
    return heights.get(skill, 100)


def confirm_dialog(screen, clock, message):
    """Show a Y/N confirmation dialog. Returns True if confirmed."""
    font = pygame.font.Font(None, 74)
    small_font = pygame.font.Font(None, 48)

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_y:
                    return True
                elif event.key == pygame.K_n or event.key == pygame.K_ESCAPE:
                    return False

        screen.fill(BLACK)

        # Message
        text = font.render(message, True, WHITE)
        screen.blit(text, (SCREEN_WIDTH // 2 - text.get_width() // 2, SCREEN_HEIGHT // 2 - 50))

        # Prompt
        prompt = small_font.render("Y = Yes, N = No", True, (150, 150, 150))
        screen.blit(prompt, (SCREEN_WIDTH // 2 - prompt.get_width() // 2, SCREEN_HEIGHT // 2 + 30))

        pygame.display.flip()
        clock.tick(60)


def skill_select_screen(screen, clock):
    """Show skill selection screen. Returns (p1_skill, p2_skill)."""
    global motor_a_delta, motor_b_delta, hub_button_pressed

    font = pygame.font.Font(None, 74)
    small_font = pygame.font.Font(None, 48)

    p1_skill = 3
    p2_skill = 3
    selecting_player = 1  # Start with P1
    last_button_state = False
    motor_accumulator = 0  # Accumulate motor movement
    motor_threshold = 15   # Degrees needed to change selection

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                exit()
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    pygame.quit()
                    exit()
                elif event.key == pygame.K_UP:
                    if selecting_player == 1:
                        p1_skill = max(1, p1_skill - 1)
                    else:
                        p2_skill = max(1, p2_skill - 1)
                elif event.key == pygame.K_DOWN:
                    if selecting_player == 1:
                        p1_skill = min(5, p1_skill + 1)
                    else:
                        p2_skill = min(5, p2_skill + 1)
                elif event.key == pygame.K_RETURN or event.key == pygame.K_SPACE:
                    if selecting_player == 1:
                        selecting_player = 2
                        motor_accumulator = 0
                    else:
                        return (p1_skill, p2_skill)

        # Read motor input (use the appropriate player's motor)
        with position_lock:
            if selecting_player == 1:
                delta = motor_a_delta
                motor_a_delta = 0
            else:
                delta = motor_b_delta
                motor_b_delta = 0
            button = hub_button_pressed

        # Accumulate motor movement
        motor_accumulator += delta
        if motor_accumulator >= motor_threshold:
            if selecting_player == 1:
                p1_skill = min(5, p1_skill + 1)
            else:
                p2_skill = min(5, p2_skill + 1)
            motor_accumulator = 0
        elif motor_accumulator <= -motor_threshold:
            if selecting_player == 1:
                p1_skill = max(1, p1_skill - 1)
            else:
                p2_skill = max(1, p2_skill - 1)
            motor_accumulator = 0

        # Hub button to confirm (edge trigger)
        if button and not last_button_state:
            if selecting_player == 1:
                selecting_player = 2
                motor_accumulator = 0
            else:
                return (p1_skill, p2_skill)
        last_button_state = button

        screen.fill(BLACK)

        # Title
        title = font.render("LEGO PONG", True, WHITE)
        screen.blit(title, (SCREEN_WIDTH // 2 - title.get_width() // 2, 100))

        # Instructions
        if selecting_player == 1:
            prompt = small_font.render("Player 1: Select Skill Level", True, WHITE)
        else:
            prompt = small_font.render("Player 2: Select Skill Level", True, WHITE)
        screen.blit(prompt, (SCREEN_WIDTH // 2 - prompt.get_width() // 2, 200))

        # Skill levels
        skill_labels = ["1 - Beginner", "2 - Easy", "3 - Normal", "4 - Hard", "5 - Expert"]
        current_skill = p1_skill if selecting_player == 1 else p2_skill

        for i, label in enumerate(skill_labels):
            color = WHITE if (i + 1) == current_skill else (100, 100, 100)
            text = small_font.render(label, True, color)
            screen.blit(text, (SCREEN_WIDTH // 2 - text.get_width() // 2, 280 + i * 50))

        # Controls hint
        hint = small_font.render("Turn paddle or UP/DOWN, then hub button or SPACE", True, (150, 150, 150))
        screen.blit(hint, (SCREEN_WIDTH // 2 - hint.get_width() // 2, SCREEN_HEIGHT - 100))

        # Show P1 selection if on P2
        if selecting_player == 2:
            p1_text = small_font.render(f"P1 Skill: {p1_skill}", True, (150, 150, 150))
            screen.blit(p1_text, (50, 200))

        pygame.display.flip()
        clock.tick(60)


def main():
    global motor_a_delta, motor_b_delta

    pygame.init()

    # Get the display size for fullscreen
    display_info = pygame.display.Info()
    global SCREEN_WIDTH, SCREEN_HEIGHT
    SCREEN_WIDTH = display_info.current_w
    SCREEN_HEIGHT = display_info.current_h

    screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT), pygame.FULLSCREEN)
    pygame.display.set_caption("Lego Pong")
    clock = pygame.time.Clock()

    # Start hub communication thread early so we can use motors in skill selection
    stop_event = threading.Event()
    hub_thread = threading.Thread(target=hub_communication_thread, args=(stop_event,))
    hub_thread.daemon = True
    hub_thread.start()

    # Skill selection
    p1_skill, p2_skill = skill_select_screen(screen, clock)
    p1_height = get_paddle_height(p1_skill)
    p2_height = get_paddle_height(p2_skill)

    font = pygame.font.Font(None, 74)
    small_font = pygame.font.Font(None, 36)

    # Create game objects with skill-adjusted paddle sizes
    paddle1 = Paddle(PADDLE_MARGIN, SCREEN_HEIGHT // 2 - p1_height // 2, p1_height)
    paddle2 = Paddle(SCREEN_WIDTH - PADDLE_MARGIN - PADDLE_WIDTH, SCREEN_HEIGHT // 2 - p2_height // 2, p2_height)
    ball = Ball(p1_skill, p2_skill)

    # Paddle Y positions (we'll update these with deltas)
    paddle1_y = SCREEN_HEIGHT // 2 - p1_height // 2
    paddle2_y = SCREEN_HEIGHT // 2 - p2_height // 2

    # Sensitivity: pixels of paddle movement per degree of motor rotation
    sensitivity = 1.5  # Higher = more sensitive

    score1 = 0
    score2 = 0

    print(f"Starting Lego Pong! P1 skill: {p1_skill}, P2 skill: {p2_skill}")
    print("Turn the motors on ports A and B to control the paddles.")
    print("Press SPACE or hub button to launch the ball.")
    print("Press N for new game, D to toggle debug info.")
    print("Press ESC or close the window to quit.")

    show_debug = False

    # Initialize button state to current state to avoid false trigger from skill selection
    with position_lock:
        last_button_state = hub_button_pressed

    # Position ball correctly before first frame
    ball.update(paddle1, paddle2)

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_SPACE:
                    ball.launch()
                elif event.key == pygame.K_d:
                    show_debug = not show_debug
                elif event.key == pygame.K_n:
                    if confirm_dialog(screen, clock, "Start New Game?"):
                        # Reset everything
                        p1_skill, p2_skill = skill_select_screen(screen, clock)
                        p1_height = get_paddle_height(p1_skill)
                        p2_height = get_paddle_height(p2_skill)
                        paddle1 = Paddle(PADDLE_MARGIN, SCREEN_HEIGHT // 2 - p1_height // 2, p1_height)
                        paddle2 = Paddle(SCREEN_WIDTH - PADDLE_MARGIN - PADDLE_WIDTH, SCREEN_HEIGHT // 2 - p2_height // 2, p2_height)
                        ball = Ball(p1_skill, p2_skill)
                        paddle1_y = SCREEN_HEIGHT // 2 - p1_height // 2
                        paddle2_y = SCREEN_HEIGHT // 2 - p2_height // 2
                        score1 = 0
                        score2 = 0
                        ball.update(paddle1, paddle2)
                        with position_lock:
                            last_button_state = hub_button_pressed

        # Get motor deltas and button state, then reset deltas
        with position_lock:
            delta_a = motor_a_delta
            delta_e = motor_b_delta
            motor_a_delta = 0
            motor_b_delta = 0
            button = hub_button_pressed

        # Launch ball on hub button press (edge trigger)
        if button and not last_button_state:
            ball.launch()
        last_button_state = button

        # Apply acceleration: small movements = precise, large movements = fast
        def accelerate(delta):
            # Quadratic acceleration - square the magnitude, keep the sign
            sign = 1 if delta >= 0 else -1
            return sign * (abs(delta) ** 1.5) * sensitivity

        # Update paddle positions based on motor movement (relative)
        paddle1_y += accelerate(delta_a)
        paddle2_y += accelerate(delta_e)

        # Clamp to screen bounds
        paddle1_y = max(0, min(SCREEN_HEIGHT - p1_height, paddle1_y))
        paddle2_y = max(0, min(SCREEN_HEIGHT - p2_height, paddle2_y))

        paddle1.move_to(int(paddle1_y))
        paddle2.move_to(int(paddle2_y))

        # Update ball
        scorer = ball.update(paddle1, paddle2)
        if scorer == 1:
            score1 += 1
            ball.attach_to_paddle(2)  # Loser (player 2) gets the ball
        elif scorer == 2:
            score2 += 1
            ball.attach_to_paddle(1)  # Loser (player 1) gets the ball

        # Draw everything
        screen.fill(BLACK)

        # Draw center line
        for y in range(0, SCREEN_HEIGHT, 30):
            pygame.draw.rect(screen, WHITE, (SCREEN_WIDTH // 2 - 2, y, 4, 15))

        paddle1.draw(screen)
        paddle2.draw(screen)
        ball.draw(screen)

        # Draw scores (offset down to avoid MacBook notch)
        score_text = font.render(f"{score1}   {score2}", True, WHITE)
        screen.blit(score_text, (SCREEN_WIDTH // 2 - score_text.get_width() // 2, 50))

        # Draw debug info (toggle with D key)
        if show_debug:
            status = "Hub Connected" if hub_connected else "Hub Disconnected - Check USB"
            status_text = small_font.render(status, True, WHITE)
            screen.blit(status_text, (10, SCREEN_HEIGHT - 40))

            debug_text = small_font.render(f"P1={int(paddle1_y)} P2={int(paddle2_y)}", True, WHITE)
            screen.blit(debug_text, (SCREEN_WIDTH - debug_text.get_width() - 10, SCREEN_HEIGHT - 40))

        pygame.display.flip()
        clock.tick(FPS)

    # Cleanup
    stop_event.set()
    hub_thread.join(timeout=1.0)
    pygame.quit()
    print("Thanks for playing!")


if __name__ == "__main__":
    main()
