# Kyuub Voxel Engine

This repository contains the backend and frontend files for **Kyuub.io**.

Made in 2026, the game mimics the mechanics of a real voxel engine and runs on a lightweight server.

---

## Table of Contents

- [How to Fork](#how-to-fork)
- [Dependencies](#dependencies)
- [How to Modify](#how-to-modify)
  - [Sending the world to the player](#sending-the-world-to-the-player)
  - [Autosaving dirty chunks](#autosaving-dirty-chunks)
  - [Handling player actions](#handling-player-actions)
  - [Making new blocks](#making-new-blocks)

---

## How to Fork

Fork the repo through Git or with the built-in **Fork** button in the top-right corner of the page.

Once forked, head to [How to Modify](#how-to-modify) for instructions on changing the full stack.

---

## Dependencies

| Package      | Purpose                         | Install                      |
| ------------ | ------------------------------- | ---------------------------- |
| Python 3.x   | Runtime                         | [python.org](https://www.python.org/downloads/) |
| `websockets` | Connects the server to the frontend | `pip install websockets` |
| `asyncio`    | Async server loop               | Built in (standard library)  |
| `random`     | Seed generation                 | Built in (standard library)  |

---

## How to Modify

With the dependencies installed, you can modify the full stack.

After forking, edit **`server.py`**. The parts that keep blocks and terrain looking clean are the **dirty** and **modified** block update types, covered below.

### Sending the world to the player

Sends the full world data to the frontend player screen.

```python
def full_world_message():
    return json.dumps({
        "type": "RENDER_DATA",
        "blocks": [
            {"x": x, "y": y, "z": z, "type": t}
            for (x, y, z), t in world.items()
        ],
    })
```

### Autosaving dirty chunks

Chunks can be marked as dirty. This loop saves the world every 5 seconds, but only when something has changed, then resets the `dirty` flag.

```python
async def autosave_loop():
    global dirty
    while True:
        await asyncio.sleep(5)
        if dirty:
            dirty = False
            await asyncio.to_thread(save_world)
```

### Handling player actions

The handler below checks each player action. Any block edit marks the world as **modified**, which also sets `dirty` to `True` so the autosave loop picks it up.

#### Movement

```python
if action == "PLAYER_MOVE":
    x, y, z = as_float(data.get("x")), as_float(data.get("y")), as_float(data.get("z"))
    yaw = as_float(data.get("yaw"), limit=1e6)
    if None in (x, y, z, yaw) or not player["announced"]:
        continue
    player["pos"] = (x, y, z)
    player["yaw"] = yaw
    await broadcast(json.dumps({
        "type": "PLAYER_STATE", "id": pid, "x": x, "y": y, "z": z, "yaw": yaw,
    }), exclude=websocket)
    continue
```

#### Making new blocks
The code below shows how to add new blocks. Both ends must be edited for this. (`server.py` and `index.html`)
```python
# Block types: 0 = air, 1 = grass, 2 = tnt, 3 = stone, 4 = wood, and 5 = dirt
BLOCK_NAMES = {0: "air", 1: "grass", 2: "tnt", 3: "stone", 4: "wood", 5:"dirt"}
VALID_TYPES = {1, 2, 3, 4, 5}
```
In `index.html`, you set the new block type in section 1. like this:
```javascript
const blockColors = {
        1: new THREE.Color(0x559933), // Grass
        2: new THREE.Color(0xcc3333), // TNT
        3: new THREE.Color(0x888888), // Stone
        4: new THREE.Color(0x775533), // Wood
	      5: new THREE.Color(0x3d251e)  // Dirt
      };
      const blockNames = {
        1: "Grass",
        2: "TNT",
        3: "Stone",
        4: "Wood",
	      5: "Dirt"
      };
```
Then in Section 4 (controls), at `document.addEventListener` keyDown event, you turn it from<br>
Digit4 to Digit5:
```html
document.addEventListener("keydown", (e) => {
        keys[e.code] = true;
        if (e.code >= "Digit1" && e.code <= "Digit5") { // <-- here
          selectedType = Number(e.code.slice(-1));
          selectedLabel.textContent = "Selected: " + blockNames[selectedType];
        }
        if (e.code === "Space") e.preventDefault();
      });
```

Remember to set your colors at Section 1 too.

#### Block edits

```python
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

dirty = True
await broadcast(json.dumps({
    "type": "BLOCK_UPDATE", "x": x, "y": y, "z": z, "block": new_type,
}))
```
