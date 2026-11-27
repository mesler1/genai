# The game logic, tested without a terminal against task.md's rules, with tests the model never saw.
set -e
python3 -c "import ast; ast.parse(open('play.py').read())"
grep -q curses play.py
python3 - <<'PY'
from collections import deque
import game


def find(rows, ch):
    return [(x, y) for y, row in enumerate(rows) for x, c in enumerate(row) if c == ch]


for seed in range(8):
    g = game.new_game(seed)
    rows = game.render(g)
    assert rows == game.render(game.new_game(seed)), "the same seed must give the same game"
    assert len(rows) == 20 and all(isinstance(r, str) and len(r) == 40 for r in rows), "20 rows of 40 characters"
    assert set("".join(rows)) <= set("#.@>m"), set("".join(rows))
    assert rows[0] == "#" * 40 and rows[-1] == "#" * 40 and all(r[0] == r[-1] == "#" for r in rows), "edge walls"
    assert sum(r[1:-1].count("#") for r in rows[1:-1]) > 0, "walls inside"
    (px, py), = find(rows, "@")
    (ex, ey), = find(rows, ">")
    assert find(rows, "m"), "some monsters"
    assert g.hp == 10 and not g.won and not g.over
    # a path to the exit: walk it, step by step, with move (monsters are only in the way, never walls)
    prev = {(px, py): None}
    todo = deque([(px, py)])
    while todo:
        x, y = todo.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            n = (x + dx, y + dy)
            if n not in prev and rows[n[1]][n[0]] != "#":
                prev[n] = (x, y)
                todo.append(n)
    assert (ex, ey) in prev, "no path to the exit"
    path, at = [], (ex, ey)
    while prev[at]:
        path.append(at)
        at = prev[at]
    hp0 = g.hp
    monsters = set(find(rows, "m"))
    hits = 0
    for x, y in reversed(path):
        hits += (x, y) in monsters
        for _ in range(2):                  # a second try, if bumping a monster left the player where it was
            at = find(game.render(g), "@")
            if not at or at[0] == (x, y) or g.over:
                break
            g = game.move(g, x - at[0][0], y - at[0][1]) or g
        if g.over:
            break
    if hits < hp0:
        assert g.won and g.over, f"seed {seed}: walking to the exit should win"
        assert g.hp == hp0 - hits, f"seed {seed}: each monster costs 1 hit point"
        frozen = game.render(g)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            g = game.move(g, dx, dy) or g
        assert game.render(g) == frozen and g.over, "nothing moves once over"
    # walls block
    g = game.new_game(seed)
    rows = game.render(g)
    (px, py), = find(rows, "@")
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        if rows[py + dy][px + dx] == "#":
            g = game.move(g, dx, dy) or g
            assert find(game.render(g), "@") == [(px, py)], "a wall must block"
            break
print("the game logic passes")
PY
[ -n "$(find . -name 'test*.py' -not -path './.git/*')" ] || { echo "no tests of its own"; exit 1; }
# its own tests run, and there are some
out=$(python3 -m unittest discover -s . -p 'test*.py' 2>&1) || { echo "$out" | tail -20; echo "its own tests fail"; exit 1; }
echo "$out" | grep -qE "^Ran [1-9]" || { echo "its own tests test nothing"; exit 1; }
grep -qiE "play\.py|python3? +play" README.md || { echo "README.md doesn't say how to play"; exit 1; }
