"""
Tests for Ember Crypt.

  1. Dungeon generation: 300 random floors; every floor tile must be reachable and everything placed sensibly
  2. Field of view: walls block sight, open rooms are fully lit
  3. A*: on random dungeons, paths must be exactly as short as a breadth-first search says is possible
  4. Bot playthroughs: an automatic player plays hundreds of full games while invariants are checked every turn
  5. Rendering: the real pygame renderer draws frames (headless) and saves screenshots

Run:  python test_ember_crypt.py
"""
import os
import random
import sys
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import ember_crypt as ec  # noqa: E402

PASSED, FAILED = [0], [0]


def check(label, ok, detail=""):
    if ok:
        PASSED[0] += 1
        print(f"  PASS  {label}")
    else:
        FAILED[0] += 1
        print(f"  FAIL  {label}  {detail}")


# ============================================================================ 1. dungeons
def test_dungeons(n=300):
    print("Dungeon generation:")
    problems = []
    rooms_total = 0
    for seed in range(n):
        w = ec.World(seed)
        for _ in range(seed % ec.FINAL_DEPTH):          # look at floors at every depth
            w.new_floor()
        floors = {(x, y) for y in range(ec.MAP_H) for x in range(ec.MAP_W) if w.tiles[y][x] == ec.FLOOR}
        reach = set(ec.bfs_distances(w.tiles, (w.player.x, w.player.y)))
        rooms_total += len(w.rooms)
        if reach != floors:
            problems.append(f"seed {seed}: {len(floors - reach)} unreachable floor tiles")
        if any(w.tiles[0][x] or w.tiles[ec.MAP_H - 1][x] for x in range(ec.MAP_W)) or \
           any(w.tiles[y][0] or w.tiles[y][ec.MAP_W - 1] for y in range(ec.MAP_H)):
            problems.append(f"seed {seed}: floor on the map edge")
        spots = [(m.x, m.y) for m in w.monsters] + [(i.x, i.y) for i in w.items] + [(w.player.x, w.player.y)]
        if len(spots) != len(set(spots)) or any(s not in floors for s in spots):
            problems.append(f"seed {seed}: overlapping or misplaced entities")
        if w.depth == ec.FINAL_DEPTH:
            if w.stairs is not None or not any(m.kind == "lich" for m in w.monsters):
                problems.append(f"seed {seed}: final floor must have the Lich and no stairs")
        elif w.stairs not in reach or w.stairs == (w.player.x, w.player.y):
            problems.append(f"seed {seed}: bad stairs")
    for p in problems[:5]:
        print("        " + p)
    check(f"{n} floors (avg {rooms_total / n:.1f} rooms): every floor tile reachable, stairs/entities placed correctly", not problems)


# ============================================================================ 2. field of view
def blank_map():
    tiles = [[ec.WALL] * ec.MAP_W for _ in range(ec.MAP_H)]
    for y in range(5, 30):
        for x in range(5, 40):
            tiles[y][x] = ec.FLOOR
    return tiles


def test_fov():
    print("\nField of view:")
    tiles = blank_map()
    vis = ec.field_of_view(tiles, 20, 15, radius=6)
    inside = {(x, y) for y in range(5, 30) for x in range(5, 40) if (x - 20) ** 2 + (y - 15) ** 2 <= 36}
    check("in an open room, every tile within the radius is visible", inside <= vis and (20, 15) in vis)
    check("nothing beyond the radius is visible", all((x - 20) ** 2 + (y - 15) ** 2 <= 36 for x, y in vis))
    for y in range(5, 30):
        tiles[y][24] = ec.WALL                         # a solid wall to the east
    vis = ec.field_of_view(tiles, 20, 15, radius=8)
    check("a solid wall blocks everything behind it", not any(x > 24 for x, y in vis) and (24, 15) in vis)
    tiles[15][24] = ec.FLOOR                           # a one-tile gap in the wall
    vis = ec.field_of_view(tiles, 20, 15, radius=8)
    check("you can see through a doorway straight ahead, but not around the corner",
          (26, 15) in vis and (27, 15) in vis and (27, 9) not in vis)


# ============================================================================ 3. A*
def test_astar(n=150):
    print("\nA* pathfinding:")
    rng = random.Random(5)
    bad = 0
    for seed in range(n):
        w = ec.World(seed)
        floors = [(x, y) for y in range(ec.MAP_H) for x in range(ec.MAP_W) if w.tiles[y][x] == ec.FLOOR]
        start = rng.choice(floors)
        dist = ec.bfs_distances(w.tiles, start)
        for goal in rng.sample(floors, 5):
            path = ec.astar(w.tiles, start, goal, limit=10 ** 6)
            ok = path is not None and len(path) == dist[goal]
            prev = start
            for step in path or []:
                ok &= ec.chebyshev(*prev, *step) == 1 and w.tiles[step[1]][step[0]] == ec.FLOOR
                prev = step
            ok &= prev == goal
            bad += not ok
    check(f"{n * 5} random paths are valid and exactly as short as breadth-first search", bad == 0, f"{bad} bad")
    tiles = blank_map()
    for y in range(5, 30):
        tiles[y][24] = ec.WALL
    check("returns None when the goal is walled off", ec.astar(tiles, (10, 10), (30, 10)) is None)


# ============================================================================ 4. bot playthroughs
def frontier_step(w):
    """Walk toward the nearest explored floor tile that touches unexplored territory."""
    p = (w.player.x, w.player.y)
    blocked = w.occupied() - {p}
    seen = {p: None}
    queue = [p]
    for cur in queue:
        x, y = cur
        if any((x + dx, y + dy) not in w.explored for dx, dy in ec.DIRS):
            while seen[cur] is not None and seen[cur] != p:
                cur = seen[cur]
            return cur if cur != p else None
        for dx, dy in ec.DIRS:
            n = (x + dx, y + dy)
            if n not in seen and n in w.explored and w.floor_at(*n) and n not in blocked:
                seen[n] = cur
                queue.append(n)
    return None


def bot_turn(w, floor_turns):
    p = w.player
    inv = w.inventory
    foes = w.visible_monsters()
    if p.hp < p.max_hp * 0.45 and "potion" in inv and w.use_item(inv.index("potion")):
        return
    if foes:
        t = foes[0]
        if len([f for f in foes if ec.chebyshev(f.x, f.y, t.x, t.y) <= 2]) >= 3 and "fireball" in inv:
            if w.use_item(inv.index("fireball")):
                return
        if t.kind in ("troll", "lich", "wraith", "orc") and "lightning" in inv and w.use_item(inv.index("lightning")):
            return
        if p.hp < p.max_hp * 0.25 and "blink" in inv and w.use_item(inv.index("blink")):
            return
        path = ec.astar(w.tiles, (p.x, p.y), (t.x, t.y), blocked=w.occupied() - {(p.x, p.y)})
        if path and w.move_player(path[0][0] - p.x, path[0][1] - p.y):
            return
        w.wait()
        return
    if w.stairs and (p.x, p.y) == w.stairs and (floor_turns > 400 or frontier_step(w) is None):
        w.descend()
        return
    step = frontier_step(w) if floor_turns <= 400 else None
    if step is None and w.stairs and w.stairs in w.explored:
        path = ec.astar(w.tiles, (p.x, p.y), w.stairs, blocked=w.occupied() - {(p.x, p.y)})
        step = path[0] if path else None
    if step is None and w.depth == ec.FINAL_DEPTH:
        lich = [m for m in w.monsters if m.kind == "lich"]
        if lich:
            path = ec.astar(w.tiles, (p.x, p.y), (lich[0].x, lich[0].y), blocked=w.occupied() - {(p.x, p.y)})
            step = path[0] if path else None
    if step is None or not w.move_player(step[0] - p.x, step[1] - p.y):
        w.wait()


def invariants(w):
    spots = [(m.x, m.y) for m in w.monsters if m.alive]
    if len(spots) != len(set(spots)) or (w.player.x, w.player.y) in spots:
        return "two things on one tile"
    if not all(w.floor_at(*s) for s in spots + [(w.player.x, w.player.y)]):
        return "something inside a wall"
    if w.player.hp > w.player.max_hp or len(w.inventory) > ec.MAX_ITEMS:
        return "stats out of range"
    if w.state == "playing" and w.player.hp <= 0:
        return "dead but still playing"
    return None


def test_bots(games=100, max_turns=5000):
    print("\nBot playthroughs:")
    t0 = time.time()
    results, errors = [], []
    for seed in range(games):
        w = ec.World(seed)
        floor_turns, last_depth = 0, 1
        try:
            while w.state == "playing" and w.turn < max_turns:
                bot_turn(w, floor_turns)
                floor_turns = 0 if w.depth != last_depth else floor_turns + 1
                last_depth = w.depth
                problem = invariants(w)
                if problem:
                    errors.append(f"seed {seed}, turn {w.turn}: {problem}")
                    break
        except Exception as e:      # noqa: BLE001 - any crash is a bug
            errors.append(f"seed {seed}: crashed with {type(e).__name__}: {e}")
        results.append(w)
    for e in errors[:5]:
        print("        " + e)
    turns = sum(w.turn for w in results)
    wins = sum(w.state == "won" for w in results)
    deaths = {}
    for w in results:
        if w.state == "dead":
            killer = next((t for t, _ in reversed(w.messages) if "slain by" in t), "?").split("slain by the ")[-1].split(" ")[0]
            deaths[killer] = deaths.get(killer, 0) + 1
    avg_depth = sum(w.depth for w in results) / games
    check(f"{games} full games ({turns:,} turns, {time.time() - t0:.0f}s): no crashes, invariants hold every turn",
          not errors)
    print(f"        the bot reached floor {avg_depth:.1f} on average and beat the Lich in {wins}/{games} runs")
    print(f"        most deaths: {', '.join(f'{k} {v}' for k, v in sorted(deaths.items(), key=lambda kv: -kv[1])[:4])}")
    check("the game is winnable but not trivial (some wins, some deaths)",
          0 < wins < games and sum(deaths.values()) > 0)


# ============================================================================ 5. rendering
def test_render():
    print("\nRendering (headless pygame):")
    import pygame
    pygame.init()
    screen = pygame.display.set_mode((ec.WIN_W, ec.WIN_H))
    w = ec.World(11)
    app = ec.App(screen, w)
    for _ in range(60):
        bot_turn(w, 0)
        app.collect_events()
        app.draw()
    keys = [pygame.K_RIGHT, pygame.K_d, pygame.K_SPACE, pygame.K_1, pygame.K_RETURN, pygame.K_q]
    for k in keys:
        app.handle_key(pygame.event.Event(pygame.KEYDOWN, key=k, unicode=""))
        app.draw()
    pygame.image.save(screen, "screenshot.png")
    w.state = "dead"
    app.draw()
    check("frames render and keyboard input works; saved screenshot.png", os.path.getsize("screenshot.png") > 10_000)


if __name__ == "__main__":
    test_dungeons()
    test_fov()
    test_astar()
    test_bots()
    test_render()
    print(f"\n{PASSED[0]} passed, {FAILED[0]} failed")
    print("ALL TESTS PASSED" if FAILED[0] == 0 else "SOME TESTS FAILED")
    sys.exit(1 if FAILED[0] else 0)
