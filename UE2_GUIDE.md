# UE2 Package Specifications (Vanguard Edition)

This directory (`renderer/ue2/`) contains our native Python implementation for parsing Unreal Engine 2 packages (`.usx`, `.vgr`, `.u`, etc.). 

## 1. Core Architecture
We do not use `umodel` for core package structure analysis. Instead, we use this library to avoid the overhead of Wine and to have direct access to the byte stream.

### Modules:
- **`reader.py`**: A robust `BinaryReader` that handles:
  - `read_compact_index()`: Essential for UE2's variable-length indices.
  - `read_fstring()`: Handles both ANSI and Unicode strings with length prefixes.
  - Basic types (`int32`, `float`, `uint16`, etc.).
- **`package.py`**: The primary `UE2Package` class. It parses the **Package Header** and populates the:
  - **Name Table**: The list of all strings used in the package.
  - **Import Table**: References to objects in other packages.
  - **Export Table**: The actual data objects contained in this package.
- **`properties.py`**: Logic for parsing the "Tagged Property" format. This is how UE2 stores object attributes (like `Location`, `StaticMesh`, `Rotation`) as a sequence of `[NameIndex][InfoByte][Size][Payload]`.
- **`types.py`**: Common Unreal types like `FVector`, `FRotator`, and `FColor`.

## 2. The Header Structure
Vanguard (Build 128/34) uses a standard UE2.5 header:
1. `Tag` (4 bytes): `0x9E2A83C1`
2. `FileVersion` (2 bytes): `128`
3. `LicenseeVersion` (2 bytes): `34` or `35`
4. `PackageFlags` (4 bytes)
5. `NameCount`, `NameOffset`
6. `ExportCount`, `ExportOffset`
7. `ImportCount`, `ImportOffset`

## 3. Usage Pattern
To use the library in a new script:

```python
from ue2 import UE2Package

pkg = UE2Package("Assets/Maps/chunk_n25_26.vgr")

# Find a specific export by name
for exp in pkg.exports:
    if "Height" in exp['object_name']:
        data = pkg.get_export_data(exp)
        # Process raw binary data...
```

## 4. Maintenance Notes
- **Compact Indices**: If you encounter an "Object Reference" in a property, remember it is a `CompactIndex`. 
  - `> 0`: Pointer to an Export.
  - `< 0`: Pointer to an Import.
  - `== 0`: NULL/None.
- **Tagged Properties**: Always use `find_property_start()` before `parse_properties()` to skip the initial object class/state metadata.
