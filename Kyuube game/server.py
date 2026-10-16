"""
Kyuub voxel server (with logging + player skins).

- Owns the world (single source of truth) and saves it to world.json
- Generates terrain once (fixed seed) the first time it runs
- Handles BREAK_BLOCK / PLACE_BLOCK from any client and broadcasts the result
- Gives every player a random skin, tracks player positions, and tells all
  other browsers where everyone is so they can render each other
- Logs everything to the console AND to kyuub.log (rotates at ~1 MB)

Install:  pip install websockets
Run:      python server.py
"""
import asyncio
import itertools
import json
import logging
import math
import os
from logging.handlers import RotatingFileHandler
import random
import requests

import websockets

HOST, PORT = "0.0.0.0", 8765
SAVE_FILE = "world.json"
LOG_FILE = "kyuub.log"
SEED = 6767
SIZE_X, SIZE_Z = 128, 64

# Block types: 0 = air, 1 = grass, 2 = tnt, 3 = stone, 4 = wood, and 5 = dirt
BLOCK_NAMES = {0: "air", 1: "grass", 2: "tnt", 3: "stone", 4: "wood", 5:"dirt"}
VALID_TYPES = {1, 2, 3, 4, 5}
MIN_XZ, MAX_XZ = -64, 96
MIN_Y, MAX_Y = 0, 64

# Skin palettes (colors are sent to the browser as hex strings)
SKIN_TONES = ["#f1c27d", "#e0ac69", "#c68642", "#8d5524", "#ffdbac"]
SHIRT_COLORS = ["#3b82f6", "#ef4444", "#22c55e", "#eab308", "#a855f7", "#f97316", "#ec4899", "#14b8a6", "FF0000"]
PANTS_COLORS = ["#1e3a8a", "#374151", "#4b5563", "#78350f", "#111827", "#996155"]


# ---------------------------------------------------------------- logging
def setup_logging():
    log = logging.getLogger("kyuub")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)-7s] %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)

    file = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file.setLevel(logging.DEBUG)
    file.setFormatter(fmt)

    log.addHandler(console)
    log.addHandler(file)
    return log


log = setup_logging()

world = {}          # (x, y, z) -> block type
players = {}        # websocket -> player dict
player_counter = itertools.count(1)
dirty = False       # world changed since last save


def desc(pos, t=None):
    x, y, z = pos
    return f"({x}, {y}, {z})" + (f" [{BLOCK_NAMES.get(t, t)}]" if t is not None else "")

def register_with_master_server(my_game_url):
    """
    Important for fetching master server URL.
    Commonly, the IP's 4th NO. is 103 or 101.
    Just enter game frontend URL on arg.
    """
    master_url = "https://ardipole.pythonanywhere.com/get-server"
    payload = {"url": my_game_url}
    
    try:
        response = requests.post(master_url, json=payload)
        print("[Master Server] Successfully updated live address")
    except Exception as e:
        print(f"[Master Server] Failed to update: {e}")


# ---------------------------------------------------------------- world
def generate_world():
    rng = random.Random(SEED)
    w = {}
    h = [[rng.randint(3, 10) for _ in range(SIZE_Z)] for _ in range(SIZE_X)]
    for _ in range(2):  # blur twice for gentler hills
        h = [[
            round(sum(h[i][j] for i in range(max(0, x - 1), min(SIZE_X, x + 2))
                              for j in range(max(0, z - 1), min(SIZE_Z, z + 2)))
                  / (len(range(max(0, x - 1), min(SIZE_X, x + 2))) *
                     len(range(max(0, z - 1), min(SIZE_Z, z + 2)))))
            for z in range(SIZE_Z)] for x in range(SIZE_X)]
    for x in range(SIZE_X):
        for z in range(SIZE_Z):
            max_y = h[x][z]
            for y in range(max_y):
                if y == max_y - 1:
                    t = 1
                elif y > max_y - 3:
                    t = 2 if (x + z) % 5 == 0 else 5
                else:
                    t = 3
                w[(x, y, z)] = t
    return w


def save_world():
    data = [[x, y, z, t] for (x, y, z), t in world.items()]
    tmp = SAVE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, SAVE_FILE)
    log.info("World saved (%d blocks) to %s", len(world), SAVE_FILE)


def load_or_create_world():
    global world
    if os.path.exists(SAVE_FILE):
        with open(SAVE_FILE) as f:
            world = {(x, y, z): t for x, y, z, t in json.load(f)}
        log.info("Loaded %d blocks from %s", len(world), SAVE_FILE)
    else:
        world = generate_world()
        log.info("No save found - generated new world (seed %d, %d blocks)", SEED, len(world))
        save_world()


async def autosave_loop():
    global dirty
    while True:
        await asyncio.sleep(5)
        if dirty:
            dirty = False
            await asyncio.to_thread(save_world)


# ---------------------------------------------------------------- players
def random_skin():
    tone = random.choice(SKIN_TONES)
    return {
        "head": tone,
        "arms": tone,
        "body": random.choice(SHIRT_COLORS),
        "legs": random.choice(PANTS_COLORS),
        "eyes": "#222222",
    }


def public_player(p):
    """What other players are allowed to know about a player."""
    pos = p["pos"] or (None, None, None)
    return {"id": p["id"], "name": p["name"], "skin": p["skin"],
            "x": pos[0], "y": pos[1], "z": pos[2], "yaw": p["yaw"]}


# ---------------------------------------------------------------- network
def full_world_message():
    return json.dumps({
        "type": "RENDER_DATA",
        "blocks": [{"x": x, "y": y, "z": z, "type": t} for (x, y, z), t in world.items()],
    })


async def broadcast(message, exclude=None):
    targets = [ws for ws in list(players) if ws is not exclude]
    if targets:
        await asyncio.gather(*(ws.send(message) for ws in targets), return_exceptions=True)


def as_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def as_float(v, limit=1000.0):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not math.isfinite(v) or abs(v) > limit:
        return None
    return float(v)


def in_bounds(x, y, z):
    return MIN_XZ <= x <= MAX_XZ and MIN_Y <= y <= MAX_Y and MIN_XZ <= z <= MAX_XZ


async def handle_client(websocket):
    global dirty
    addr = websocket.remote_address[0] if websocket.remote_address else "?"
    pid = next(player_counter)
    player = {"id": pid, "name": f"Player{pid}", "skin": random_skin(),
              "pos": None, "yaw": 0.0, "announced": False}
    players[websocket] = player
    label = f"{player['name']} ({addr})"
    log.info("JOIN  %s connected - %d online", label, len(players))

    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("BAD   %s sent invalid JSON", label)
                continue
            if not isinstance(data, dict):
                log.warning("BAD   %s sent a non-object message", label)
                continue
            action = data.get("action")
            if action != "PLAYER_MOVE":              # movement is too spammy to log
                log.debug("RECV  %s: %s", label, raw)

            # ---- world request
            if action == "GENERATE_CHUNK":
                await websocket.send(full_world_message())
                log.info("WORLD %s requested the world (%d blocks sent)", label, len(world))
                continue

            # ---- skin / player setup
            if action == "CREATE_PLAYER_SKIN":
                others = [public_player(p) for ws, p in players.items()
                          if ws is not websocket and p["announced"]]
                await websocket.send(json.dumps({
                    "type": "PLAYER_INIT", "id": pid, "name": player["name"],
                    "skin": player["skin"], "players": others,
                }))
                if not player["announced"]:
                    player["announced"] = True
                    await broadcast(json.dumps({
                        "type": "PLAYER_JOIN", "id": pid, "name": player["name"],
                        "skin": player["skin"], "x": None, "y": None, "z": None, "yaw": 0.0,
                    }), exclude=websocket)
                log.info("SKIN  %s got skin %s (%d other player(s) sent)", label, player["skin"], len(others))
                continue

            if action == "PLAYER_MOVE":
                x, y, z = as_float(data.get("x")), as_float(data.get("y")), as_float(data.get("z"))
                yaw = as_float(data.get("yaw"), limit=1e6)
                pitch = as_float(data.get("pitch"), limit=10.0) or 0.0
                if None in (x, y, z, yaw) or not player["announced"]:
                    continue
                player["pos"] = (x, y, z)
                player["yaw"] = yaw
                player["pitch"] = pitch
                await broadcast(json.dumps({
                    "type": "PLAYER_STATE", "id": pid, "x": x, "y": y, "z": z, "yaw": yaw, "pitch": pitch,
                }), exclude=websocket)
                continue

            # ---- block edits
            if action not in ("BREAK_BLOCK", "PLACE_BLOCK"):
                log.warning("BAD   %s sent unknown action %r", label, action)
                continue

            x, y, z = as_int(data.get("x")), as_int(data.get("y")), as_int(data.get("z"))
            if None in (x, y, z):
                log.warning("DENY  %s %s - invalid coordinates %r", label, action,
                            (data.get("x"), data.get("y"), data.get("z")))
                continue
            pos = (x, y, z)
            if not in_bounds(x, y, z):
                log.warning("DENY  %s %s %s - out of bounds", label, action, desc(pos))
                continue

            if action == "BREAK_BLOCK":
                old = world.get(pos)
                if old is None:
                    log.info("DENY  %s BREAK %s - nothing there", label, desc(pos))
                    continue
                del world[pos]
                new_type = 0
                log.info("BREAK %s broke %s", label, desc(pos, old))
                log.info("DEBUG: %s", old)
                if old == 2 or old == "2":
                    log.info("ACTION %s broke tnt, centered 17x17 'explosion' update sent to client(s).", label)
                    x, y, z = pos
                    
                    # Loop from -8 to +8 to perfectly center the explosion on the TNT block
                    for dx in range(-8, 9):
                        for dz in range(-8, 9):
                            # Skip the exact center block because the default break logic handles it
                            if dx == 0 and dz == 0:
                                continue
                                
                            target_pos = (x + dx, y, z + dz)
                            
                            if target_pos in world:
                                del world[target_pos]
                                
                                await broadcast(json.dumps({
                                    "type": "BLOCK_UPDATE", 
                                    "x": x + dx, 
                                    "y": y, 
                                    "z": z + dz, 
                                    "block": 0,
                                }))


            else:  # PLACE_BLOCK
                t = as_int(data.get("type"))
                if t not in VALID_TYPES:
                    log.warning("DENY  %s PLACE %s - invalid block type %r", label, desc(pos), data.get("type"))
                    continue
                if pos in world:
                    log.info("DENY  %s PLACE %s - spot occupied by %s", label, desc(pos), BLOCK_NAMES[world[pos]])
                    continue
                world[pos] = t
                new_type = t
                log.info("PLACE %s placed %s", label, desc(pos, t))
                log.info("DEBUG: %s", t)

            dirty = True
            await broadcast(json.dumps({
                "type": "BLOCK_UPDATE", "x": x, "y": y, "z": z, "block": new_type,
            }))
            log.info("UPDATE %s -> %s sent to %d client(s)", desc(pos), BLOCK_NAMES[new_type], len(players))
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception:
        log.exception("ERROR while handling %s", label)
    finally:
        players.pop(websocket, None)
        if player["announced"]:
            await broadcast(json.dumps({"type": "PLAYER_LEAVE", "id": pid}))
        log.info("LEAVE %s disconnected - %d online", label, len(players))


async def main():
    log.info("=== Kyuub server starting ===")
    load_or_create_world()
    saver = asyncio.create_task(autosave_loop())
    try:
        async with websockets.serve(handle_client, HOST, PORT):
            log.info("Listening on %s:%d", HOST, PORT)
            await asyncio.Future()
    finally:
        saver.cancel()
        save_world()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("=== Server stopped by user ===")
