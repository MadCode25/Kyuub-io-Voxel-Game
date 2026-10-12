# Kyuub Voxel Engine
<p>This repository is what contains the backend files and frontend files of Kyuub.io.<br>Made in 2026, the game mimics the mechanics<br>and has a lightweight server.</p>
<hr></hr>

# How to fork
<br>
<hr>
<p>The repo can be forked through Git or the built-in Fork button<br>at the top-right corner. Go to the "How to Modify?" tab for instructions on how<br>to modify the full stack.</p>

# Dependencies
<p>Python:</p>
<strong>Python 3.xx and pip</strong>
<br>
<strong>asyncio (install thru pip)</strong>
<br>
<strong>websockets (to connect to frontend)</strong>
<br>
<strong>random (usually built-in, for seed gen)</strong>
<br>

# How to Modify?
<br>
<hr>
<br>
<p>Since you have installed all the dependencies, you can now modify the full stack.</p> <br>
<p>After forking the repo, you have to edit the server.py file. The parts needed to make<br>the blocks and terrain look clean are "dirty" and "modified" block update types.</p>
<br>
<h1>Example code (backend):</h1>
<p>
  # The code sends full world data to the frontend player screen
  def full_world_message():
    return json.dumps({
        "type": "RENDER_DATA",
        "blocks": [{"x": x, "y": y, "z": z, "type": t} for (x, y, z), t in world.items()],
    })
</p>
<br>
<p>You can also mark dirty chunks. Like this:</p>
<br>
<p>
  # This one ONLY marks the dirty chunk to false if the player just joined or hasn't been interacted with
  async def autosave_loop():
    global dirty
    while True:
        await asyncio.sleep(5)
        if dirty:
            dirty = False
            await asyncio.to_thread(save_world)
  # And this one marks the chunk to "modified", and this also turns dirty to true.
  # By the way, this is the "movements" section. Basically checks the player's actions.
  # ---- movement
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

</p>
