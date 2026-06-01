"""
Parse VG.sql (vgo-server emulator MySQL dump) and produce
output/data/npc_spawns.json:
{
    "chunk_n25_26": [
        {"spawn_id": 10000, "location_id": 1,
         "x": 71041.65, "y": -93500.20, "z": 49995.05,    # Vanguard world coords
         "pitch": 0, "yaw": 16384, "roll": 0,             # UE2 rotation units
         "spawn_chance": 100},
        ...
    ],
    ...
}

Joins three tables:
    chunks                       (chunk_id -> filename)
    unreal_location_placements   (location_id -> chunk_id, x, y, z, pitch, yaw, roll)
    unreal_location_entry        (location_id -> spawn_id)
"""
import json
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SQL_PATH = os.path.join(PROJECT_ROOT, "VG.sql")
OUT_PATH = os.path.join(PROJECT_ROOT, "output", "data", "npc_spawns.json")


def parse_value_tuple(text):
    """Parse a single VALUES tuple `( ... )` into a list of strings/numbers.

    Handles quoted strings with escaped quotes and commas inside strings.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        # skip whitespace
        while i < n and text[i] in " \t":
            i += 1
        if i >= n:
            break
        if text[i] == "'":
            # quoted string
            j = i + 1
            buf = []
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                elif text[j] == "'":
                    break
                else:
                    buf.append(text[j])
                    j += 1
            out.append("".join(buf))
            i = j + 1
        else:
            # number/literal until comma
            j = i
            while j < n and text[j] != ",":
                j += 1
            tok = text[i:j].strip()
            if tok.upper() == "NULL":
                out.append(None)
            else:
                try:
                    out.append(int(tok))
                except ValueError:
                    try:
                        out.append(float(tok))
                    except ValueError:
                        out.append(tok)
            i = j
        # consume trailing comma
        while i < n and text[i] in ", \t":
            i += 1
    return out


def iter_table_rows(sql_path, table_name):
    """Yield row tuples from `INSERT INTO `table_name`` statements.

    Streams the file; collects the multi-row INSERT body and splits on
    top-level `),(` boundaries.
    """
    pat_start = re.compile(rf"^INSERT INTO `{re.escape(table_name)}` ")
    with open(sql_path, "r", encoding="utf-8", errors="replace") as fh:
        in_block = False
        buf = []
        for line in fh:
            if not in_block:
                if pat_start.match(line):
                    in_block = True
                    # the VALUES portion may begin on this line after ") VALUES"
                    idx = line.find(" VALUES")
                    if idx >= 0:
                        buf.append(line[idx + len(" VALUES"):])
                    else:
                        buf.append(line)
                continue
            buf.append(line)
            stripped = line.rstrip()
            if stripped.endswith(";"):
                # End of insert block
                body = "".join(buf)
                # Trim trailing semicolon and surrounding whitespace
                body = body.strip().rstrip(";").strip()
                # Split on `),(` boundaries
                # The first row starts after `(`, last ends with `)`.
                # Use a regex split that ignores boundaries inside quoted strings.
                rows = _split_rows(body)
                for raw in rows:
                    yield parse_value_tuple(raw)
                in_block = False
                buf = []


def _split_rows(body):
    """Split the body of a VALUES (...),(...),... statement into row strings.

    Tracks quote state to avoid splitting inside strings.
    """
    rows = []
    depth = 0
    in_quote = False
    start = None
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if in_quote:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == "'":
                in_quote = False
        else:
            if c == "'":
                in_quote = True
            elif c == "(":
                if depth == 0:
                    start = i + 1
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0 and start is not None:
                    rows.append(body[start:i])
                    start = None
        i += 1
    return rows


def main():
    if not os.path.exists(SQL_PATH):
        print(f"ERROR: {SQL_PATH} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing {SQL_PATH} ...")

    # Pass 1: chunks (chunk_id -> filename)
    chunk_id_to_name = {}
    for row in iter_table_rows(SQL_PATH, "chunks"):
        # columns: chunk_id, continent, shortname, displayname, filename, ...
        if len(row) >= 5:
            chunk_id_to_name[row[0]] = row[4]
    print(f"  chunks: {len(chunk_id_to_name)}")

    # Pass 2: placements (location_id -> chunk_id, x/y/z/yaw/...)
    placements = {}
    for row in iter_table_rows(SQL_PATH, "unreal_location_placements"):
        # columns: location_id_fk, chunk_id_fk, location_x, location_y, location_z,
        #          x_offset, y_offset, z_offset, rotation_pitch, rotation_yaw,
        #          rotation_roll, ...
        if len(row) < 11:
            continue
        loc_id = row[0]
        placements[loc_id] = {
            "chunk_id": row[1],
            "x": row[2],
            "y": row[3],
            "z": row[4],
            "pitch": row[8],
            "yaw": row[9],
            "roll": row[10],
        }
    print(f"  placements: {len(placements)}")

    # Pass 3: location entries (spawn_id -> location_id)
    out = {}  # chunk_filename -> list of spawn dicts
    spawn_count = 0
    for row in iter_table_rows(SQL_PATH, "unreal_location_entry"):
        # columns: id, location_id_fk, spawn_id_fk, spawn_chance, script_id_fk, last_updated
        if len(row) < 4:
            continue
        loc_id = row[1]
        spawn_id = row[2]
        chance = row[3]
        place = placements.get(loc_id)
        if place is None:
            continue
        chunk_name = chunk_id_to_name.get(place["chunk_id"])
        if not chunk_name:
            continue
        out.setdefault(chunk_name, []).append(
            {
                "spawn_id": spawn_id,
                "location_id": loc_id,
                "x": place["x"],
                "y": place["y"],
                "z": place["z"],
                "pitch": place["pitch"],
                "yaw": place["yaw"],
                "roll": place["roll"],
                "spawn_chance": chance,
            }
        )
        spawn_count += 1

    print(f"  spawns: {spawn_count} across {len(out)} chunks")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as fh:
        json.dump(out, fh)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
