"""
Kyuub voxel server - chunk streaming edition.

- The world is split into 16x16 columns ("chunks") generated on demand from the
  seed, so a 256x256 world is never built or sent all at once
- Only player EDITS are saved (world_edits.json), so the save file stays tiny
- Each player is sent only the chunks near them; block updates only go to
  players who have that chunk loaded
- Skins, player positions, chat and TNT explosions work as before

Install:  pip install websockets
Run:      python server.py
"""
import asyncio
import base64
import itertools
import json
import logging
import math
import os
import ssl
import random
import zlib
from logging.handlers import RotatingFileHandler

import websockets

HOST, PORT = "0.0.0.0", 9081
SAVE_FILE = "world_edits.json"
LOG_FILE = "kyuub.log"
SEED = 6767

CHUNK = 16
WORLD_SIZE = 256            # blocks per side (x and z), a multiple of 16
HEIGHT = 64                 # blocks tall, a multiple of 16 (client WORLD_HEIGHT must match)
WORLD_CHUNKS = WORLD_SIZE // CHUNK
LOAD_DIST = 3               # chunks sent around the player (the client draws 2)
SPAWN = (8.0, 20.0, 25.0)   # where streaming starts before the player has moved

# Block types: 0 = air, 1 = grass, 2 = tnt, 3 = stone, 4 = wood, 5 = dirt, 6 = tree leaves (leaves in-game)
BLOCK_NAMES = {0: "air", 1: "grass", 2: "tnt", 3: "stone", 4: "wood", 5: "dirt", 6: "leaves", 7: "water"}
VALID_TYPES = {1, 2, 3, 4, 5, 6, 7}
SEA_LEVEL = 14        # higher = MORE LAKES LAEEKKS

SKIN_TONES = ["#f1c27d", "#e0ac69", "#c68642", "#8d5524", "#ffdbac"]
SHIRT_COLORS = ["#3b82f6", "#ef4444", "#22c55e", "#eab308", "#a855f7", "#f97316", "#ec4899", "#14b8a6", "#FF0000"]
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

players = {}                # websocket -> player dict
player_counter = itertools.count(1)
chunk_cache = {}            # (cx, cz) -> bytearray of block types
encoded_cache = {}          # (cx, cz) -> ready-to-send JSON message
edits = {}                  # (cx, cz) -> {(x, y, z): type}   (the only thing that gets saved)
dirty = False               # edits changed since the last save


def desc(pos, t=None):
    x, y, z = pos
    return f"({x}, {y}, {z})" + (f" [{BLOCK_NAMES.get(t, t)}]" if t is not None else "")


# ---------------------------------------------------------------- helpers
def chunk_in_world(cx, cz):
    return 0 <= cx < WORLD_CHUNKS and 0 <= cz < WORLD_CHUNKS


def in_bounds(x, y, z):
    return 0 <= x < WORLD_SIZE and 0 <= z < WORLD_SIZE and 0 <= y < HEIGHT


def block_index(lx, y, lz):
    return lx + CHUNK * (lz + CHUNK * y)       # y-major: empty sky compresses to almost nothing


def chunk_of(v):
    """Chunk coordinate for a position (blocks are centered on whole numbers)."""
    return math.floor(v + 0.5) // CHUNK


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


# ---------------------------------------------------------------- terrain
def hash2(ix, iz):
    """Deterministic pseudo-random number 0..1 for an integer grid point."""
    n = (ix * 374761393 + iz * 668265263 + SEED * 1442695041) & 0xFFFFFFFF
    n = ((n ^ (n >> 13)) * 1274126177) & 0xFFFFFFFF
    n ^= n >> 16
    return n / 0xFFFFFFFF


def value_noise(x, z, scale):
    x /= scale
    z /= scale
    x0, z0 = math.floor(x), math.floor(z)
    fx, fz = x - x0, z - z0
    fx, fz = fx * fx * (3 - 2 * fx), fz * fz * (3 - 2 * fz)      # smoothstep
    a, b = hash2(x0, z0), hash2(x0 + 1, z0)
    c, d = hash2(x0, z0 + 1), hash2(x0 + 1, z0 + 1)
    top = a + (b - a) * fx
    bottom = c + (d - c) * fx
    return top + (bottom - top) * fz


def terrain_height(x, z):
    n = 0.65 * value_noise(x, z, 40) + 0.25 * value_noise(x, z, 16) + 0.10 * value_noise(x, z, 6)
    return min(HEIGHT - 1, int(6 + n * 22))


def block_for(y, surface):
    """Which block goes at height y in a column whose top is at `surface`. Edit to taste."""
    if y == surface - 1:
        return 1            # grass on top
    if y >= surface - 4:
        return 5            # dirt below it
    if y >= surface - random.uniform(4, 5):
        return 2

    return 3                # stone underneath

	


TREE_CHANCE = 0.012     # about 1.2% of columns get a pillar
TREE_HEIGHT = 5         # logs per pillar

def generate_chunk(cx, cz):
    chunk = bytearray(CHUNK * CHUNK * HEIGHT)
    if not chunk_in_world(cx, cz):
        return chunk
    for lz in range(CHUNK):
        for lx in range(CHUNK):
            x, z = cx * CHUNK + lx, cz * CHUNK + lz
            surface = terrain_height(x, z)
            for y in range(surface):
                chunk[block_index(lx, y, lz)] = block_for(y, surface)
	                # lakes: fill everything below sea level with water, with dirt underneath
            for y in range(surface, SEA_LEVEL):
                chunk[block_index(lx, y, lz)] = 7
            if surface < SEA_LEVEL:
                chunk[block_index(lx, surface - 1, lz)] = 5     # no grass underwater

            # log pillar with a grass block on top, standing on the ground
            if hash2(x + 50000, z - 50000) < TREE_CHANCE and surface + TREE_HEIGHT < HEIGHT:
                for i in range(TREE_HEIGHT):
                    chunk[block_index(lx, surface + i, lz)] = 4          # wood
                chunk[block_index(lx, surface + TREE_HEIGHT, lz)] = 6    # grass on top
    return chunk


# ---------------------------------------------------------------- world storage
def get_chunk(cx, cz):
    key = (cx, cz)
    chunk = chunk_cache.get(key)
    if chunk is None:
        chunk = generate_chunk(cx, cz)
        for (x, y, z), t in edits.get(key, {}).items():      # re-apply saved player edits
            chunk[block_index(x - cx * CHUNK, y, z - cz * CHUNK)] = t
        chunk_cache[key] = chunk
    return chunk


def get_block(x, y, z):
    if not in_bounds(x, y, z):
        return 0
    cx, cz = x // CHUNK, z // CHUNK
    return get_chunk(cx, cz)[block_index(x - cx * CHUNK, y, z - cz * CHUNK)]


def set_block(x, y, z, t):
    global dirty
    cx, cz = x // CHUNK, z // CHUNK
    get_chunk(cx, cz)[block_index(x - cx * CHUNK, y, z - cz * CHUNK)] = t
    edits.setdefault((cx, cz), {})[(x, y, z)] = t
    encoded_cache.pop((cx, cz), None)       # the cached message is stale now
    dirty = True


def chunk_message(cx, cz):
    key = (cx, cz)
    msg = encoded_cache.get(key)
    if msg is None:
        packed = zlib.compress(bytes(get_chunk(cx, cz)), 6)
        msg = json.dumps({"type": "CHUNK_DATA", "cx": cx, "cz": cz,
                          "data": base64.b64encode(packed).decode("ascii")})
        encoded_cache[key] = msg
    return msg


# ---------------------------------------------------------------- saving
def snapshot_edits():
    return [[x, y, z, t] for chunk_edits in edits.values() for (x, y, z), t in chunk_edits.items()]


def write_save(flat):
    tmp = SAVE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"seed": SEED, "edits": flat}, f)
    os.replace(tmp, SAVE_FILE)
    log.info("Saved %d edit(s) to %s", len(flat), SAVE_FILE)


def load_world():
    if not os.path.exists(SAVE_FILE):
        log.info("No save file - starting with a fresh world (seed %d)", SEED)
        return
    with open(SAVE_FILE) as f:
        data = json.load(f)
    if data.get("seed") != SEED:
        log.warning("%s was made with seed %s but SEED is %s - ignoring the old edits",
                    SAVE_FILE, data.get("seed"), SEED)
        return
    for x, y, z, t in data["edits"]:
        edits.setdefault((x // CHUNK, z // CHUNK), {})[(x, y, z)] = t
    log.info("Loaded %d saved edit(s) from %s", len(data["edits"]), SAVE_FILE)


async def autosave_loop():
    global dirty
    while True:
        await asyncio.sleep(5)
        if dirty:
            dirty = False
            flat = snapshot_edits()                       # copy on the main thread...
            await asyncio.to_thread(write_save, flat)     # ...write on another


# ---------------------------------------------------------------- players
def random_skin():
    tone = random.choice(SKIN_TONES)
    return {"head": tone, "arms": tone, "body": random.choice(SHIRT_COLORS),
            "legs": random.choice(PANTS_COLORS), "eyes": "#222222"}


def public_player(p):
    pos = p["pos"] or (None, None, None)
    return {"id": p["id"], "name": p["name"], "skin": p["skin"],
            "x": pos[0], "y": pos[1], "z": pos[2], "yaw": p["yaw"], "pitch": p["pitch"]}


# ---------------------------------------------------------------- network
async def broadcast(message, exclude=None):
    targets = [ws for ws in list(players) if ws is not exclude]
    if targets:
        await asyncio.gather(*(ws.send(message) for ws in targets), return_exceptions=True)


async def send_block_updates(updates):
    """updates = [(x, y, z, type), ...]. Each player only gets blocks inside chunks they have loaded."""
    sends = []
    for ws, p in list(players.items()):
        mine = [[x, y, z, t] for x, y, z, t in updates if (x // CHUNK, z // CHUNK) in p["loaded"]]
        if mine:
            sends.append(ws.send(json.dumps({"type": "BLOCK_UPDATES", "updates": mine})))
    if sends:
        await asyncio.gather(*sends, return_exceptions=True)


async def update_streaming(websocket, player, x, z):
    """Send new chunks near the player and tell the browser to drop far ones.
    Does nothing unless the player has crossed into a different chunk."""
    pcx, pcz = chunk_of(x), chunk_of(z)
    if (pcx, pcz) == (player["pcx"], player["pcz"]):
        return
    player["pcx"], player["pcz"] = pcx, pcz
    loaded = player["loaded"]

    far = [c for c in loaded if max(abs(c[0] - pcx), abs(c[1] - pcz)) > LOAD_DIST + 1]
    if far:
        loaded.difference_update(far)
        await websocket.send(json.dumps({"type": "UNLOAD_CHUNKS", "chunks": far}))

    wanted = [(cx, cz)
              for cx in range(pcx - LOAD_DIST, pcx + LOAD_DIST + 1)
              for cz in range(pcz - LOAD_DIST, pcz + LOAD_DIST + 1)
              if chunk_in_world(cx, cz) and (cx, cz) not in loaded]
    wanted.sort(key=lambda c: (c[0] - pcx) ** 2 + (c[1] - pcz) ** 2)     # nearest first
    for c in wanted:
        loaded.add(c)          # mark as loaded BEFORE sending, so no edit can slip between
        await websocket.send(chunk_message(*c))
        await asyncio.sleep(0)  # let other players' messages through between chunks
    if wanted:
        log.info("STREAM %s now has %d chunk(s) loaded (+%d, -%d)",
                 player["name"], len(loaded), len(wanted), len(far))


async def handle_client(websocket):
    addr = websocket.remote_address[0] if websocket.remote_address else "?"
    pid = next(player_counter)
    player = {"id": pid, "name": f"Player{pid}", "skin": random_skin(),
              "pos": None, "yaw": 0.0, "pitch": 0.0, "announced": False,
              "loaded": set(), "pcx": None, "pcz": None}
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
            if action != "PLAYER_MOVE":
                log.debug("RECV  %s: %s", label, raw)

            # ---- start streaming the world around the spawn point
            if action == "GENERATE_CHUNK":
                await update_streaming(websocket, player, SPAWN[0], SPAWN[2])
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
                        "skin": player["skin"], "x": None, "y": None, "z": None,
                        "yaw": 0.0, "pitch": 0.0,
                    }), exclude=websocket)
		    
                log.info("SKIN  %s got skin %s (%d other player(s) sent)", label, player["skin"], len(others))
                continue

            # ---- movement (also drives chunk streaming)
            if action == "PLAYER_MOVE":
                x, y, z = as_float(data.get("x")), as_float(data.get("y")), as_float(data.get("z"))
                yaw = as_float(data.get("yaw"), limit=1e6)
                pitch = as_float(data.get("pitch"), limit=10.0) or 0.0
                if None in (x, y, z, yaw) or not player["announced"]:
                    continue
                player["pos"], player["yaw"], player["pitch"] = (x, y, z), yaw, pitch
                await update_streaming(websocket, player, x, z)
                await broadcast(json.dumps({
                    "type": "PLAYER_STATE", "id": pid, "x": x, "y": y, "z": z, "yaw": yaw, "pitch": pitch,
                }), exclude=websocket)
                continue

            # ---- chat
            if action == "PLAYER_MESSAGE":
                text = data.get("msg")
                if not isinstance(text, str):
                    log.warning("BAD   %s sent a non-text chat message", label)
                    continue
                text = text.strip()[:200]
                if not text:
                    continue
                log.info("CHAT  %s: %s", label, text)
                await broadcast(json.dumps({"type": "CHAT", "name": player["name"], "msg": text}))
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
                old = get_block(x, y, z)
                if not old or old == 7:
                    log.info("DENY  %s BREAK %s - nothing there", label, desc(pos))
                    continue
                set_block(x, y, z, 0)
                updates = [(x, y, z, 0)]
                log.info("BREAK %s broke %s", label, desc(pos, old))
                if old == 2:    # TNT: clear a 17x17 square on the same level
                    log.info("ACTION %s broke tnt, 17x17 explosion", label)
                    for dx in range(-8, 9):
                        for dz in range(-8, 9):
                            tx, tz = x + dx, z + dz
                            if (dx or dz) and get_block(tx, y, tz):
                                set_block(tx, y, tz, 0)
                                updates.append((tx, y, tz, 0))
            else:  # PLACE_BLOCK
                t = as_int(data.get("type"))
                if t not in VALID_TYPES:
                    log.warning("DENY  %s PLACE %s - invalid block type %r", label, desc(pos), data.get("type"))
                    continue
                existing = get_block(x, y, z)
                if existing and existing != 7:
                    log.info("DENY  %s PLACE %s - spot occupied by %s", label, desc(pos), BLOCK_NAMES.get(existing))
                    continue
                set_block(x, y, z, t)
                updates = [(x, y, z, t)]
                log.info("PLACE %s placed %s", label, desc(pos, t))

            await send_block_updates(updates)
            log.info("UPDATE %d block(s) changed", len(updates))
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception:
        log.exception("ERROR while handling %s", label)
    finally:
        players.pop(websocket, None)
        if player["announced"]:
            await broadcast(json.dumps({"type": "PLAYER_LEAVE", "id": pid}))
        log.info("LEAVE %s disconnected - %d online", label, len(players))






# ---------------------------------------------------------------- server lifecycle
async def main():
    # Load world data before opening the network layer
    load_world()                                      

    # Start your background database/world autosave loop
    saver = asyncio.create_task(autosave_loop())
    
    try:
        async with websockets.serve(handle_client, "0.0.0.0", 9080):
            log.info("Kyuub server listening locally on ws://0.0.0.0:9080")
            await asyncio.Future()
    finally:
        # Clean up and force write world snapshot to disk if the server closes
        saver.cancel()
        write_save(snapshot_edits())

    

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Server stopped manually.")

