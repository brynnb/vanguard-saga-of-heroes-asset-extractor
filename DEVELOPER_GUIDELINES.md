# Vanguard Developer Guidelines

**Canonical Database**: `/Users/brynnbateman/Downloads/Vanguard EMU/vanguard_files.db`
- This is the ONLY database to use for persistent parsing status and binary metrics.
- All parsed data, unknown regions, and observations go here.

**Recommended Parsing Library**: `construct` (https://pypi.org/project/construct/)
- Use `parse_stream()` instead of `parse()` to track consumed bytes.

---

## 1. Documentation First Policy (CRITICAL)

**We do not write code without updating documentation.** Every discovery regarding Vanguard's binary data structures must be recorded in the relevant guide immediately.

**Rules for Documentation**:
1. **Never use placeholders**: If data is unknown, mark it as `Unknown_{offset}`.
2. **Synchronization**: Code and Docs must match. If the code implements a 34-pixel shift, the corresponding `.md` file must explain *why*.
3. **No "Shadow Knowledge"**: Do not rely on conversation history. If it isn't in a `.md` file, it doesn't exist for the next session.
4. **Binary Layouts**: Documentation MUST include detailed tables showing offsets, field names, types, and sizes for data structures discovered in the original client assets.

---

## 2. Core Parsing Philosophy

**We are reverse-engineering a closed-source game engine.** Every byte matters. We do not guess, skip, or use heuristics. A file is only considered "fully parsed" when every single byte from offset 0 to EOF has been read and assigned to a named field or structure.

### 2.1 Documentation Synchronization Matrix

Every parsing discovery MUST be documented in the corresponding domain guide:

| Data Category | Documentation File | Primary Parser |
|---------------|-------------------|----------------|
| **Packages / Headers** | `UE2_GUIDE.md` | `ue2/package.py` |
| **Terrain / VGR** | `TERRAIN_GUIDE.md` | `extractors/extract_all_terrain.py` |
| **Meshes / USX** | `MESH_GUIDE.md` | `lib/staticmesh_construct.py` |
| **World / Prefabs** | `OBJECTS_GUIDE.md` | `extractors/extract_chunk_data.py` |
| **Engine Source** | `ENGINE_SOURCE_GUIDE.md` | N/A (Reference) |

---

## 3. Unknown Data Handling

When encountering bytes we don't understand:
1. **Always read them** - never skip.
2. **Name them systematically**: `unknown_0x{offset}`.
3. **Log observed values** across multiple files to find patterns.
4. **Store raw bytes** in the database for later analysis.

---

## 4. Testing and Validation

1. **Round-trip validation**: Can we read then write the file back identically?
2. **Multi-file validation**: Does the parser work on at least 5 different files of the same type?
3. **Offset verification**: After parsing, `reader.tell() == len(data)` (everything consumed).
4. **Human Feedback**: Do not attempt to test things for yourself in the browser. Rely on the human user to provide visual verification and feedback.

---

## 5. File Hygiene

- **Analysis scripts** go in `renderer/_analysis/` during active investigation.
- **Completed findings** must be moved to guides or the `_archive/` folder.
- Never commit scratch files or redirected logs (like `notify.log`) to the repository root.

---

## 6. Checklist for Parser Completion

- [ ] Every byte from 0 to EOF is read and named.
- [ ] No hardcoded skips or seeks without file-internal justification.
- [ ] All unknown fields are documented in the relevant `*_GUIDE.md`.
- [ ] Parser tested on 5+ different files.
- [ ] `reader.tell() == len(data)` after parsing.
- [ ] Database tables (`parsed_exports`, `unknown_regions`) populated.
