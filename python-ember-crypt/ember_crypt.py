"""
EMBER CRYPT: a roguelike dungeon crawler.

Descend 8 randomly generated floors, fight what lives down there, and destroy the Lich at the bottom.
Every floor is new: rooms are carved with BSP, you see by torchlight (shadowcasting field of view),
and monsters hunt you with A* pathfinding.

Controls
    Arrows / WASD / QEZC (diagonals) / numpad   move or attack
    Space or .                                  wait a turn
    Enter or >                                  go down the stairs
    1-9                                         use an item
    R                                           new game (after death or victory)
    Esc                                         quit

Requires: pip install pygame
"""
import heapq
import math
import random
import sys
from collections import deque

# ============================================================================ constants
MAP_W, MAP_H = 64, 40
TILE = 16
HUD_H = 132
WIN_W, WIN_H = MAP_W * TILE, MAP_H * TILE + HUD_H
FOV_RADIUS = 9
WALL, FLOOR = 0, 1
FINAL_DEPTH = 8
MAX_ITEMS = 9

#            glyph  color            hp atk def  xp  first depth
MONSTERS = {
    "rat":      ("r", (186, 150, 110),  6,  3, 0,   4, 1),
    "bat":      ("b", (170, 120, 210),  5,  4, 0,   5, 1),
    "goblin":   ("g", (110, 210, 90),  12,  5, 1,  10, 2),
    "skeleton": ("s", (225, 225, 210), 14,  6, 1,  16, 3),
    "orc":      ("o", (70, 180, 70),   20,  7, 2,  26, 4),
    "wraith":   ("W", (150, 185, 255), 18,  8, 2,  34, 5),
    "troll":    ("T", (40, 150, 100),  32,  9, 3,  55, 6),
    "lich":     ("L", (255, 90, 210),  75, 11, 4, 200, FINAL_DEPTH),
}
ITEMS = {
    "potion":    ("!", (255, 80, 90),   "Healing Potion"),
    "fireball":  ("?", (255, 150, 40),  "Scroll of Fireball"),
    "lightning": ("?", (120, 200, 255), "Scroll of Lightning"),
    "blink":     ("?", (200, 130, 255), "Scroll of Blink"),
    "sword":     (")", (240, 230, 120), "Sharper Blade"),
    "armor":     ("[", (170, 175, 210), "Sturdier Armor"),
}
DIRS = {(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)}


def chebyshev(ax, ay, bx, by):
    return max(abs(ax - bx), abs(ay - by))


# ============================================================================ entities
class Entity:
    def __init__(self, x, y, kind, hp, atk, defense, xp=0):
        self.x, self.y, self.kind = x, y, kind
        self.hp = self.max_hp = hp
        self.atk, self.defense, self.xp = atk, defense, xp
        self.alert = False
        self.cooldown = 0

    @property
    def alive(self):
        return self.hp > 0

    @property
    def glyph(self):
        return "@" if self.kind == "player" else MONSTERS[self.kind][0]

    @property
    def color(self):
        return (255, 235, 180) if self.kind == "player" else MONSTERS[self.kind][1]

    @property
    def name(self):
        return "you" if self.kind == "player" else "the " + self.kind


class Item:
    def __init__(self, x, y, kind):
        self.x, self.y, self.kind = x, y, kind

    @property
    def glyph(self):
        return ITEMS[self.kind][0]

    @property
    def color(self):
        return ITEMS[self.kind][1]

    @property
    def name(self):
        return ITEMS[self.kind][2]


# ============================================================================ dungeon generation (BSP)
def generate_dungeon(rng, w=MAP_W, h=MAP_H):
    """Binary space partitioning: split the map into boxes, put a room in each leaf, and join siblings
    with corridors. Joining siblings bottom-up means every room is guaranteed to be reachable."""
    tiles = [[WALL] * w for _ in range(h)]
    rooms = []

    def carve_room(rx, ry, rw, rh):
        for y in range(ry, ry + rh):
            for x in range(rx, rx + rw):
                tiles[y][x] = FLOOR
        rooms.append((rx, ry, rw, rh))
        return [(rx, ry, rw, rh)]

    def carve_corridor(a, b):
        (x1, y1), (x2, y2) = center(a), center(b)
        horizontal_first = rng.random() < 0.5
        for (sx, sy, ex, ey) in ([(x1, y1, x2, y1), (x2, y1, x2, y2)] if horizontal_first
                                 else [(x1, y1, x1, y2), (x1, y2, x2, y2)]):
            for x in range(min(sx, ex), max(sx, ex) + 1):
                for y in range(min(sy, ey), max(sy, ey) + 1):
                    tiles[y][x] = FLOOR

    def build(x, y, bw, bh, depth):
        can_split_x, can_split_y = bw >= 18, bh >= 14
        if depth >= 5 or not (can_split_x or can_split_y) or (depth >= 3 and rng.random() < 0.2):
            rw = rng.randint(4, min(bw - 2, 13))
            rh = rng.randint(3, min(bh - 2, 8))
            return carve_room(x + rng.randint(1, bw - rw - 1), y + rng.randint(1, bh - rh - 1), rw, rh)
        split_x = can_split_x and (not can_split_y or bw / bh > 1.2 or (bh / bw <= 1.2 and rng.random() < 0.5))
        if split_x:
            cut = rng.randint(9, bw - 9)
            a, b = build(x, y, cut, bh, depth + 1), build(x + cut, y, bw - cut, bh, depth + 1)
        else:
            cut = rng.randint(7, bh - 7)
            a, b = build(x, y, bw, cut, depth + 1), build(x, y + cut, bw, bh - cut, depth + 1)
        carve_corridor(rng.choice(a), rng.choice(b))
        return a + b

    build(0, 0, w, h, 0)
    for _ in range(rng.randint(1, 3)):              # a few extra corridors make loops, so fights can be escaped
        carve_corridor(rng.choice(rooms), rng.choice(rooms))
    return tiles, rooms


def center(room):
    rx, ry, rw, rh = room
    return rx + rw // 2, ry + rh // 2


def bfs_distances(tiles, start):
    dist = {start: 0}
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in DIRS:
            n = (x + dx, y + dy)
            if n not in dist and 0 <= n[0] < MAP_W and 0 <= n[1] < MAP_H and tiles[n[1]][n[0]] == FLOOR:
                dist[n] = dist[(x, y)] + 1
                queue.append(n)
    return dist


# ============================================================================ field of view (recursive shadowcasting)
OCTANTS = [(1, 0, 0, -1), (0, 1, -1, 0), (0, -1, -1, 0), (-1, 0, 0, -1),
           (-1, 0, 0, 1), (0, -1, 1, 0), (0, 1, 1, 0), (1, 0, 0, 1)]


def field_of_view(tiles, ox, oy, radius=FOV_RADIUS):
    """Scan 8 triangular octants row by row. When a wall is hit, the slopes it hides are remembered and
    everything behind it stays dark. Cost is proportional to the visible area, not the whole map."""
    visible = {(ox, oy)}

    def opaque(x, y):
        return not (0 <= x < MAP_W and 0 <= y < MAP_H) or tiles[y][x] == WALL

    def cast(row, start, end, xx, xy, yx, yy):
        if start < end:
            return
        new_start = start
        for j in range(row, radius + 1):
            dx, dy = -j - 1, -j
            blocked = False
            while dx <= 0:
                dx += 1
                x, y = ox + dx * xx + dy * xy, oy + dx * yx + dy * yy
                l_slope, r_slope = (dx - 0.5) / (dy + 0.5), (dx + 0.5) / (dy - 0.5)
                if start < r_slope:
                    continue
                if end > l_slope:
                    break
                if dx * dx + dy * dy <= radius * radius and 0 <= x < MAP_W and 0 <= y < MAP_H:
                    visible.add((x, y))
                if blocked:
                    if opaque(x, y):
                        new_start = r_slope
                    else:
                        blocked = False
                        start = new_start
                elif opaque(x, y) and j < radius:
                    blocked = True
                    cast(j + 1, start, l_slope, xx, xy, yx, yy)
                    new_start = r_slope
            if blocked:
                break

    for xx, xy, yx, yy in OCTANTS:
        cast(1, 1.0, 0.0, xx, xy, yx, yy)
    return visible


def line_clear(tiles, x0, y0, x1, y1):
    """Bresenham line of sight (used for the Lich's bolts)."""
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    x, y = x0, y0
    while (x, y) != (x1, y1):
        if (x, y) != (x0, y0) and tiles[y][x] == WALL:
            return False
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy
    return True


# ============================================================================ A* pathfinding
def astar(tiles, start, goal, blocked=frozenset(), limit=4000):
    """Shortest 8-directional path (every step costs 1, so the Chebyshev distance is an exact-when-open
    heuristic). Returns the list of steps after `start`, or None. `blocked` are occupied tiles."""
    if start == goal:
        return []
    open_heap = [(chebyshev(*start, *goal), 0, start)]
    came = {start: None}
    g = {start: 0}
    expanded = 0
    while open_heap:
        _, cost, cur = heapq.heappop(open_heap)
        if cur == goal:
            path = []
            while cur != start:
                path.append(cur)
                cur = came[cur]
            return path[::-1]
        if cost > g[cur]:
            continue
        expanded += 1
        if expanded > limit:
            return None
        x, y = cur
        for dx, dy in DIRS:
            n = (x + dx, y + dy)
            if not (0 <= n[0] < MAP_W and 0 <= n[1] < MAP_H) or tiles[n[1]][n[0]] == WALL:
                continue
            if n in blocked and n != goal:
                continue
            ng = cost + 1
            if ng < g.get(n, 1 << 30):
                g[n] = ng
                came[n] = cur
                heapq.heappush(open_heap, (ng + chebyshev(*n, *goal), ng, n))
    return None


# ============================================================================ the game world (no graphics here)
class World:
    def __init__(self, seed=None):
        self.seed = seed if seed is not None else random.randrange(1 << 30)
        self.rng = random.Random(self.seed)
        self.player = Entity(0, 0, "player", 32, 6, 1)
        self.player.level, self.player.next_xp = 1, 30
        self.inventory = ["potion"]
        self.messages = deque(maxlen=60)
        self.events = []            # things the renderer should animate: ("text", x, y, str, color), ("bolt", ...)
        self.state = "playing"
        self.depth = 0
        self.turn = 0
        self.kills = 0
        self.log("You descend into the Ember Crypt. Somewhere below, the Lich waits.", (255, 200, 120))
        self.new_floor()

    # ---------------------------------------------------------------- helpers
    def log(self, text, color=(220, 220, 220)):
        self.messages.append((text, color))

    def floor_at(self, x, y):
        return 0 <= x < MAP_W and 0 <= y < MAP_H and self.tiles[y][x] == FLOOR

    def monster_at(self, x, y):
        for m in self.monsters:
            if m.alive and m.x == x and m.y == y:
                return m
        return None

    def occupied(self):
        return {(m.x, m.y) for m in self.monsters if m.alive} | {(self.player.x, self.player.y)}

    def free_tile_in(self, room, taken):
        rx, ry, rw, rh = room
        for _ in range(50):
            p = (self.rng.randint(rx, rx + rw - 1), self.rng.randint(ry, ry + rh - 1))
            if p not in taken:
                return p
        return None

    @property
    def score(self):
        return self.depth * 100 + self.kills * 10 + self.player.level * 50 + (1000 if self.state == "won" else 0)

    # ---------------------------------------------------------------- floors
    def new_floor(self):
        self.depth += 1
        self.tiles, self.rooms = generate_dungeon(self.rng)
        start_room = self.rooms[0]
        self.player.x, self.player.y = center(start_room)
        dist = bfs_distances(self.tiles, (self.player.x, self.player.y))
        far_room = max(self.rooms[1:], key=lambda r: dist.get(center(r), -1))
        self.stairs = None if self.depth == FINAL_DEPTH else center(far_room)
        self.monsters, self.items = [], []
        taken = {(self.player.x, self.player.y)} | ({self.stairs} if self.stairs else set())
        if self.depth == FINAL_DEPTH:
            lx, ly = center(far_room)
            self.monsters.append(self.make_monster("lich", lx, ly))
            taken.add((lx, ly))
            self.log("A cold laugh echoes through the halls. The Lich is here.", (255, 120, 220))
        for room in self.rooms[1:]:
            for _ in range(self.rng.randint(0, min(1 + self.depth // 2, 4))):
                p = self.free_tile_in(room, taken)
                if p:
                    taken.add(p)
                    self.monsters.append(self.make_monster(self.pick_monster(), *p))
            if self.rng.random() < 0.55:
                p = self.free_tile_in(room, taken)
                if p:
                    taken.add(p)
                    self.items.append(Item(*p, self.pick_item()))
        self.explored = set()
        self.update_fov()

    def make_monster(self, kind, x, y):
        _, _, hp, atk, df, xp, _ = MONSTERS[kind]
        scale = max(0, self.depth - MONSTERS[kind][6])           # tougher when met deeper than usual
        return Entity(x, y, kind, hp + scale * 3, atk + scale, df + scale // 3, xp + scale * 2)

    def pick_monster(self):
        options = [k for k, v in MONSTERS.items() if v[6] <= self.depth and k != "lich"]
        weights = [1 + 2 * (MONSTERS[k][6] >= self.depth - 2) for k in options]   # favour the newest monsters
        return self.rng.choices(options, weights)[0]

    def pick_item(self):
        return self.rng.choices(list(ITEMS), [40, 14, 14, 10, 8, 8])[0]

    def update_fov(self):
        self.visible = field_of_view(self.tiles, self.player.x, self.player.y)
        self.explored |= self.visible

    # ---------------------------------------------------------------- combat
    def attack(self, attacker, defender):
        dmg = max(1, attacker.atk + self.rng.randint(-2, 2) - defender.defense)
        crit = self.rng.random() < 0.1
        if crit:
            dmg *= 2
        defender.hp -= dmg
        color = (255, 90, 90) if defender is self.player else (255, 230, 120)
        self.events.append(("text", defender.x, defender.y, f"{dmg}{'!' if crit else ''}", color))
        if defender is self.player:
            self.events.append(("shake", min(12, 3 + dmg)))
            self.log(f"The {attacker.kind} hits you for {dmg}{' (critical!)' if crit else ''}.", (255, 140, 140))
            if not self.player.alive:
                self.state = "dead"
                self.log(f"You were slain by the {attacker.kind} on floor {self.depth}.", (255, 80, 80))
        else:
            self.log(f"You hit the {defender.kind} for {dmg}{' (critical!)' if crit else ''}.")
            defender.alert = True
            if not defender.alive:
                self.monster_died(defender)

    def damage_monster(self, m, dmg, color):
        m.hp -= dmg
        m.alert = True
        self.events.append(("text", m.x, m.y, str(dmg), color))
        if not m.alive:
            self.monster_died(m)

    def monster_died(self, m):
        self.kills += 1
        self.events.append(("burst", m.x, m.y, m.color))
        self.log(f"The {m.kind} dies. (+{m.xp} XP)", (160, 255, 160))
        if m.kind == "lich":
            self.state = "won"
            self.log("The Lich crumbles to ash. The crypt falls silent. You won!", (255, 220, 120))
        self.gain_xp(m.xp)

    def gain_xp(self, amount):
        p = self.player
        p.xp += amount
        while p.xp >= p.next_xp:
            p.xp -= p.next_xp
            p.level += 1
            p.next_xp = int(p.next_xp * 1.45)
            p.max_hp += 8
            p.hp = min(p.max_hp, p.hp + 12)
            p.atk += 1
            if p.level % 2 == 0:
                p.defense += 1
            self.events.append(("text", p.x, p.y, "LEVEL UP", (120, 255, 200)))
            self.log(f"You feel stronger! You are now level {p.level}.", (120, 255, 200))

    # ---------------------------------------------------------------- player actions (each returns True if it took a turn)
    def move_player(self, dx, dy):
        if self.state != "playing":
            return False
        nx, ny = self.player.x + dx, self.player.y + dy
        target = self.monster_at(nx, ny)
        if target:
            self.attack(self.player, target)
            return self.end_turn()
        if not self.floor_at(nx, ny):
            return False
        self.player.x, self.player.y = nx, ny
        for item in list(self.items):
            if (item.x, item.y) == (nx, ny):
                self.pick_up(item)
        if self.stairs == (nx, ny):
            self.log("Stairs lead deeper down. Press Enter to descend.", (255, 210, 120))
        return self.end_turn()

    def pick_up(self, item):
        if item.kind in ("sword", "armor"):
            self.items.remove(item)
            if item.kind == "sword":
                self.player.atk += 2
                self.log("You take a sharper blade. (+2 attack)", (240, 230, 120))
            else:
                self.player.defense += 1
                self.player.max_hp += 4
                self.player.hp += 4
                self.log("You strap on sturdier armor. (+1 defense, +4 max HP)", (170, 175, 210))
        elif len(self.inventory) < MAX_ITEMS:
            self.items.remove(item)
            self.inventory.append(item.kind)
            self.log(f"You pick up a {item.name}.", item.color)
        else:
            self.log("Your pack is full.", (200, 160, 160))

    def wait(self):
        return self.state == "playing" and self.end_turn()

    def descend(self):
        if self.state != "playing":
            return False
        if self.stairs != (self.player.x, self.player.y):
            self.log("There are no stairs here.", (180, 180, 180))
            return False
        self.player.hp = min(self.player.max_hp, self.player.hp + self.player.max_hp // 4)
        self.new_floor()
        self.log(f"You descend to floor {self.depth}." + (" This is the bottom." if self.depth == FINAL_DEPTH else ""),
                 (255, 210, 120))
        return True

    def visible_monsters(self):
        return sorted((m for m in self.monsters if m.alive and (m.x, m.y) in self.visible),
                      key=lambda m: chebyshev(m.x, m.y, self.player.x, self.player.y))

    def use_item(self, index):
        if self.state != "playing" or not 0 <= index < len(self.inventory):
            return False
        kind = self.inventory[index]
        p = self.player
        if kind == "potion":
            if p.hp == p.max_hp:
                self.log("You are already at full health.", (180, 180, 180))
                return False
            heal = max(12, p.max_hp * 2 // 5)
            p.hp = min(p.max_hp, p.hp + heal)
            self.events.append(("text", p.x, p.y, f"+{heal}", (120, 255, 140)))
            self.log(f"You drink the potion and recover {heal} HP.", (255, 120, 130))
        elif kind in ("fireball", "lightning"):
            targets = self.visible_monsters()
            if not targets:
                self.log("There is nothing to target.", (180, 180, 180))
                return False
            t = targets[0]
            if kind == "lightning":
                self.events.append(("bolt", p.x, p.y, t.x, t.y, (140, 210, 255)))
                self.log(f"Lightning strikes the {t.kind}!", (140, 210, 255))
                self.damage_monster(t, 22 + self.depth * 3, (140, 210, 255))
            else:
                self.events.append(("blast", t.x, t.y, 2))
                self.log("A fireball explodes!", (255, 160, 60))
                for m in [m for m in self.monsters if m.alive and chebyshev(m.x, m.y, t.x, t.y) <= 2]:
                    self.damage_monster(m, 16 + self.depth * 2, (255, 160, 60))
        elif kind == "blink":
            spots = [(x, y) for (x, y) in self.explored if self.floor_at(x, y) and (x, y) not in self.occupied()
                     and chebyshev(x, y, p.x, p.y) >= 6]
            if not spots:
                self.log("The scroll fizzles.", (180, 180, 180))
            else:
                self.events.append(("burst", p.x, p.y, (200, 130, 255)))
                p.x, p.y = self.rng.choice(spots)
                self.log("You blink across the crypt.", (200, 130, 255))
        self.inventory.pop(index)
        return self.end_turn()

    # ---------------------------------------------------------------- monsters' turn
    def end_turn(self):
        self.turn += 1
        if self.turn % 7 == 0 and self.player.alive:
            self.player.hp = min(self.player.max_hp, self.player.hp + 1)     # slow natural healing
        self.update_fov()
        if self.state == "playing":
            for m in self.monsters:
                if m.alive and self.state == "playing":
                    self.monster_act(m)
            self.monsters = [m for m in self.monsters if m.alive]
        self.update_fov()
        return True

    def monster_act(self, m):
        p = self.player
        if m.kind == "troll" and m.hp < m.max_hp:
            m.hp += 1                                                       # trolls regenerate
        sees = (m.x, m.y) in self.visible
        if sees:
            m.alert = True
        if not m.alert:
            if self.rng.random() < 0.25:
                self.step_monster(m, *self.rng.choice(list(DIRS)))
            return
        dist = chebyshev(m.x, m.y, p.x, p.y)
        if m.kind == "lich" and sees and dist > 1:
            m.cooldown -= 1
            if m.cooldown <= 0 and line_clear(self.tiles, m.x, m.y, p.x, p.y):
                m.cooldown = 3
                dmg = max(1, 9 + self.rng.randint(0, 4) - p.defense // 2)
                p.hp -= dmg
                self.events.append(("bolt", m.x, m.y, p.x, p.y, (255, 90, 210)))
                self.events.append(("text", p.x, p.y, str(dmg), (255, 90, 90)))
                self.events.append(("shake", 8))
                self.log(f"The Lich hurls a bolt of shadow for {dmg}!", (255, 120, 220))
                if not p.alive:
                    self.state = "dead"
                    self.log("You were slain by the Lich.", (255, 80, 80))
                return
        if m.kind == "bat" and self.rng.random() < 0.4:                     # bats flutter erratically
            self.step_monster(m, *self.rng.choice(list(DIRS)))
            return
        if dist == 1:
            self.attack(m, p)
            return
        path = astar(self.tiles, (m.x, m.y), (p.x, p.y), blocked=self.occupied() - {(m.x, m.y)}, limit=1500)
        if path:
            nx, ny = path[0]
            if (nx, ny) != (p.x, p.y) and not self.monster_at(nx, ny):
                m.x, m.y = nx, ny

    def step_monster(self, m, dx, dy):
        nx, ny = m.x + dx, m.y + dy
        if self.floor_at(nx, ny) and (nx, ny) not in self.occupied():
            m.x, m.y = nx, ny


# ============================================================================ graphics (pygame)
def run_game(seed=None):
    import pygame

    pygame.init()
    pygame.display.set_caption("Ember Crypt")
    screen = pygame.display.set_mode((WIN_W, WIN_H))
    app = App(screen, World(seed))
    clock = pygame.time.Clock()
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                return
            if event.type == pygame.KEYDOWN and not app.handle_key(event):
                pygame.quit()
                return
        app.draw()
        pygame.display.flip()
        clock.tick(60)


class App:
    def __init__(self, screen, world):
        import pygame
        self.pg = pygame
        self.screen = screen
        self.world = world
        self.font = pygame.font.SysFont("consolas,dejavusansmono,couriernew", 16, bold=True)
        self.small = pygame.font.SysFont("consolas,dejavusansmono,couriernew", 14)
        self.big = pygame.font.SysFont("consolas,dejavusansmono,couriernew", 48, bold=True)
        self.glyphs = {}
        self.floaters, self.effects = [], []
        self.shake = 0
        self.frame = 0
        self.keys = {
            pygame.K_UP: (0, -1), pygame.K_DOWN: (0, 1), pygame.K_LEFT: (-1, 0), pygame.K_RIGHT: (1, 0),
            pygame.K_w: (0, -1), pygame.K_s: (0, 1), pygame.K_a: (-1, 0), pygame.K_d: (1, 0),
            pygame.K_q: (-1, -1), pygame.K_e: (1, -1), pygame.K_z: (-1, 1), pygame.K_c: (1, 1),
            pygame.K_KP8: (0, -1), pygame.K_KP2: (0, 1), pygame.K_KP4: (-1, 0), pygame.K_KP6: (1, 0),
            pygame.K_KP7: (-1, -1), pygame.K_KP9: (1, -1), pygame.K_KP1: (-1, 1), pygame.K_KP3: (1, 1),
        }

    def glyph(self, ch, color):
        key = (ch, color)
        if key not in self.glyphs:
            self.glyphs[key] = self.font.render(ch, True, color)
        return self.glyphs[key]

    def handle_key(self, event):
        pg, w = self.pg, self.world
        if event.key == pg.K_ESCAPE:
            return False
        if w.state != "playing":
            if event.key == pg.K_r:
                self.world = World()
                self.floaters, self.effects = [], []
            return True
        if event.key in self.keys:
            w.move_player(*self.keys[event.key])
        elif event.key in (pg.K_SPACE, pg.K_PERIOD, pg.K_KP5):
            w.wait()
        elif event.key in (pg.K_RETURN, pg.K_KP_ENTER) or event.unicode == ">":
            if w.descend():
                self.floaters, self.effects = [], []       # old effects belong to the floor above
        elif pg.K_1 <= event.key <= pg.K_9:
            w.use_item(event.key - pg.K_1)
        self.collect_events()
        return True

    def collect_events(self):
        for ev in self.world.events:
            if ev[0] == "text":
                self.floaters.append([ev[1] * TILE + TILE // 2, ev[2] * TILE, ev[3], ev[4], 50])
            elif ev[0] == "shake":
                self.shake = max(self.shake, ev[1])
            else:
                self.effects.append([ev, 22])
        self.world.events.clear()

    # ---------------------------------------------------------------- drawing
    def light(self, x, y):
        p = self.world.player
        d = math.hypot(x - p.x, y - p.y)
        flicker = 0.04 * math.sin(self.frame * 0.15) + 0.03 * math.sin(self.frame * 0.37)
        return max(0.18, min(1.0, 1.08 - d / (FOV_RADIUS + 1.5) + flicker))

    def draw(self):
        pg, w = self.pg, self.world
        self.frame += 1
        canvas = pg.Surface((WIN_W, MAP_H * TILE))
        canvas.fill((6, 5, 8))
        for y in range(MAP_H):
            for x in range(MAP_W):
                if (x, y) not in w.explored:
                    continue
                seen = (x, y) in w.visible
                noise = ((x * 7919 + y * 104729) % 13) - 6
                rect = (x * TILE, y * TILE, TILE, TILE)
                if w.tiles[y][x] == WALL:
                    if seen:
                        k = self.light(x, y)
                        base = (int((92 + noise) * k), int((66 + noise) * k), int(54 * k))
                        pg.draw.rect(canvas, base, rect)
                        pg.draw.rect(canvas, tuple(min(255, int(c * 1.35)) for c in base), (x * TILE, y * TILE, TILE, 3))
                    else:
                        pg.draw.rect(canvas, (30, 30, 42), rect)
                else:
                    if seen:
                        k = self.light(x, y)
                        pg.draw.rect(canvas, (int((38 + noise) * k), int((30 + noise) * k), int(26 * k)), rect)
                        if (x + y) % 3 == 0:
                            pg.draw.rect(canvas, (int(60 * k), int(48 * k), int(40 * k)), (x * TILE + 7, y * TILE + 7, 2, 2))
                    else:
                        pg.draw.rect(canvas, (14, 14, 22), rect)
        if w.stairs and w.stairs in w.explored:
            canvas.blit(self.glyph(">", (255, 210, 90)), (w.stairs[0] * TILE + 3, w.stairs[1] * TILE - 1))
        for item in w.items:
            if (item.x, item.y) in w.visible:
                canvas.blit(self.glyph(item.glyph, item.color), (item.x * TILE + 3, item.y * TILE - 1))
        for m in w.monsters:
            if m.alive and (m.x, m.y) in w.visible:
                canvas.blit(self.glyph(m.glyph, m.color), (m.x * TILE + 3, m.y * TILE - 1))
                if m.hp < m.max_hp:
                    pg.draw.rect(canvas, (60, 0, 0), (m.x * TILE + 1, m.y * TILE + 14, TILE - 2, 2))
                    pg.draw.rect(canvas, (230, 60, 60), (m.x * TILE + 1, m.y * TILE + 14, int((TILE - 2) * m.hp / m.max_hp), 2))
        p = w.player
        glow = pg.Surface((TILE * 3, TILE * 3), pg.SRCALPHA)
        pg.draw.circle(glow, (255, 170, 60, 40 + int(12 * math.sin(self.frame * 0.2))), (TILE * 3 // 2, TILE * 3 // 2), TILE * 3 // 2)
        canvas.blit(glow, (p.x * TILE - TILE, p.y * TILE - TILE))
        canvas.blit(self.glyph("@", p.color), (p.x * TILE + 3, p.y * TILE - 1))
        self.draw_effects(canvas)

        ox = oy = 0
        if self.shake:
            ox, oy = random.randint(-self.shake, self.shake), random.randint(-self.shake, self.shake)
            self.shake = max(0, self.shake - 1)
        self.screen.fill((0, 0, 0))
        self.screen.blit(canvas, (ox, oy))
        self.draw_hud()
        if w.state != "playing":
            self.draw_end_screen()

    def draw_effects(self, canvas):
        pg = self.pg
        for fx in self.effects:
            ev, life = fx
            t = life / 22
            if ev[0] == "bolt":
                _, x0, y0, x1, y1, color = ev
                pts = [(x0 * TILE + 8, y0 * TILE + 8)]
                for i in range(1, 8):                       # jagged lightning between the two tiles
                    f = i / 8
                    pts.append((x0 * TILE + 8 + (x1 - x0) * TILE * f + random.randint(-5, 5),
                                y0 * TILE + 8 + (y1 - y0) * TILE * f + random.randint(-5, 5)))
                pts.append((x1 * TILE + 8, y1 * TILE + 8))
                pg.draw.lines(canvas, color, False, pts, max(1, int(3 * t)))
            elif ev[0] == "blast":
                _, x, y, r = ev
                surf = pg.Surface((TILE * (2 * r + 1),) * 2, pg.SRCALPHA)
                pg.draw.circle(surf, (255, 140, 40, int(170 * t)), (surf.get_width() // 2,) * 2, int(surf.get_width() / 2 * (1.2 - t * 0.4)))
                canvas.blit(surf, ((x - r) * TILE, (y - r) * TILE))
            elif ev[0] == "burst":
                _, x, y, color = ev
                for i in range(10):
                    a = i / 10 * math.tau
                    rr = (1 - t) * 18
                    pg.draw.circle(canvas, color, (int(x * TILE + 8 + math.cos(a) * rr), int(y * TILE + 8 + math.sin(a) * rr)), 2)
            fx[1] -= 1
        self.effects = [fx for fx in self.effects if fx[1] > 0]
        for f in self.floaters:
            surf = self.small.render(f[2], True, f[3])
            surf.set_alpha(min(255, f[4] * 8))
            canvas.blit(surf, (f[0] - surf.get_width() // 2, f[1] - 4))
            f[1] -= 0.6
            f[4] -= 1
        self.floaters = [f for f in self.floaters if f[4] > 0]

    def bar(self, x, y, w, h, frac, fg, bg, label):
        pg = self.pg
        pg.draw.rect(self.screen, bg, (x, y, w, h), border_radius=4)
        pg.draw.rect(self.screen, fg, (x, y, int(w * max(0.0, min(1.0, frac))), h), border_radius=4)
        self.screen.blit(self.small.render(label, True, (255, 255, 255)), (x + 8, y + 1))

    def draw_hud(self):
        pg, w, p = self.pg, self.world, self.world.player
        top = MAP_H * TILE
        pg.draw.rect(self.screen, (16, 13, 18), (0, top, WIN_W, HUD_H))
        pg.draw.line(self.screen, (120, 70, 40), (0, top), (WIN_W, top), 2)
        self.bar(12, top + 12, 230, 18, p.hp / p.max_hp, (200, 50, 60), (60, 20, 24), f"HP {max(0, p.hp)}/{p.max_hp}")
        self.bar(12, top + 36, 230, 14, p.xp / p.next_xp, (90, 170, 255), (20, 34, 60), f"LV {p.level}  XP {p.xp}/{p.next_xp}")
        stats = f"Floor {w.depth}/{FINAL_DEPTH}   ATK {p.atk}   DEF {p.defense}   Kills {w.kills}   Turn {w.turn}"
        self.screen.blit(self.small.render(stats, True, (220, 200, 170)), (12, top + 58))
        self.screen.blit(self.small.render("Pack (press 1-9 to use):", True, (180, 160, 140)), (12, top + 74))
        for i, kind in enumerate(w.inventory):
            label = f"{i + 1} {ITEMS[kind][2]}"
            self.screen.blit(self.small.render(label, True, ITEMS[kind][1]), (12 + (i % 3) * 190, top + 90 + (i // 3) * 13))
        msgs = list(w.messages)[-6:]
        for i, (text, color) in enumerate(msgs):
            fade = 0.45 + 0.55 * (i + 1) / len(msgs)
            surf = self.small.render(text[:58], True, tuple(int(c * fade) for c in color))
            self.screen.blit(surf, (590, top + 10 + i * 19))

    def draw_end_screen(self):
        pg, w = self.pg, self.world
        shade = pg.Surface((WIN_W, MAP_H * TILE), pg.SRCALPHA)
        shade.fill((0, 0, 0, 170))
        self.screen.blit(shade, (0, 0))
        won = w.state == "won"
        title = self.big.render("VICTORY" if won else "YOU DIED", True, (255, 210, 110) if won else (230, 60, 70))
        self.screen.blit(title, title.get_rect(center=(WIN_W // 2, MAP_H * TILE // 2 - 40)))
        lines = [f"Floor {w.depth}   Level {w.player.level}   Kills {w.kills}   Turns {w.turn}",
                 f"Score {w.score}", "Press R for a new run, Esc to quit"]
        for i, line in enumerate(lines):
            s = self.font.render(line, True, (230, 220, 200))
            self.screen.blit(s, s.get_rect(center=(WIN_W // 2, MAP_H * TILE // 2 + 20 + i * 26)))


if __name__ == "__main__":
    run_game(int(sys.argv[1]) if len(sys.argv) > 1 else None)
