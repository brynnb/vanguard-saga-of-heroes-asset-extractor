# Vanguard Developer Guidelines

**Canonical Database**: `output/data/vanguard_data.db`
- This is the ONLY database to use for all parsed data.
- All extracted data, unknown regions, and observations go here.
- Use `config.DB_PATH` in code to reference this database.

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
| **Terrain / VGR** | `TERRAIN_GUIDE.md` | `scripts/extractors/extract_all_terrain.py` |
| **Meshes / USX** | `MESH_GUIDE.md` | `scripts/lib/staticmesh_construct.py` |
| **World / Prefabs** | `OBJECTS_GUIDE.md` | `scripts/extractors/extract_chunk_data.py` |
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

---

## 7. No Bandaid Fixes (CRITICAL)

**We do not suppress, hide, or work around errors.** If something is broken, we fix the root cause. Covering up problems creates hidden technical debt and makes debugging harder.

### Prohibited Practices

1. **Returning empty data to avoid crashes**: If a database table doesn't exist, the server must return a clear error message (e.g., `{"error": "Table 'files' not found. Run 'python3 setup.py' to initialize."}`), not an empty list.

2. **Catching and silently ignoring exceptions**: If an exception occurs, either handle it properly or let it propagate with a descriptive message.

3. **Conditional feature hiding**: If a feature requires data that doesn't exist, show an actionable error—don't hide the feature.

4. **"Good enough" heuristics without documentation**: If we must use a heuristic or approximation, document why and track the affected cases.

### Required Practices

1. **Fail fast with actionable messages**: Tell the user exactly what went wrong and how to fix it.

2. **Validate preconditions explicitly**: Check that required tables, files, and configurations exist at startup.

3. **Track known issues**: If a root cause fix is deferred, create a GitHub issue and reference it in the code.

---

## 8. Unified Setup Pipeline (CRITICAL)

**All extraction and indexing scripts must be integrated into `setup.py`.** Users should never need to run individual scripts manually during normal setup.

### Rules

1. **Single entry point**: Running `python3 setup.py` must produce a fully working database and all derived files.

2. **New scripts require setup.py integration**: When adding a new extractor or indexer, add it to the appropriate stage in `setup.py`.

3. **Pipeline stages** (current order):
   - Stage 1: Validate configuration
   - Stage 2: Initialize database schema
   - Stage 3: Index asset files
   - Stage 4: Export chunk data
   - Stage 5: Index mesh objects
   - Stage 6: Build texture database
   - Stage 7: Extract object properties (`extract_properties.py`)
   - Stage 8: Full extraction (optional, `--full` flag)

4. **Reset capability**: Use `--reset` flag to delete the database and start fresh.

5. **No manual script running**: If you find yourself telling users to "run script X manually," that script should be added to `setup.py`.

---

## 9. Documentation Preservation (CRITICAL)

**Never delete correct information from documentation.** When refactoring or updating guides, preserve all valid content. Deleting working documentation forces re-discovery of already-solved problems.

### Rules

1. **Verify before deleting**: Before removing documentation, confirm the information is actually incorrect by checking the current working code.

2. **Update, don't replace**: When fixing incorrect information, update the specific incorrect parts rather than rewriting entire sections.

3. **Preserve debugging tips**: Symptom-cause tables and debugging advice took real effort to discover—keep them.

4. **Mark deprecated, don't delete**: If a technique is no longer needed (e.g., a manual shift now handled by the parser), move it to a "Historical Notes" section rather than deleting.

5. **Code is truth**: If documentation conflicts with working code, the working code is correct. Update the docs to match the code, not the other way around.

### Examples of What NOT to Do

- ❌ Deleting the 256-boundary heuristic documentation because "we're not sure why it works"
- ❌ Removing mesh generation code examples when updating texture parsing
- ❌ Overwriting the entire TERRAIN_GUIDE when only the marker selection logic changed
- ❌ Deleting debugging symptom tables because "we fixed the bug"

### What TO Do

- ✅ Fix incorrect values (e.g., change "Big-Endian" to "Little-Endian") in-place
- ✅ Add new sections while preserving existing correct content
- ✅ Move deprecated techniques to a "Historical/Deprecated" section with explanation
- ✅ Cross-reference code when documenting to ensure accuracy
