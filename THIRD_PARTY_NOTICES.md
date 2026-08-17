# Third-Party Notices

## UE Viewer

Some UE2 package, property, and Vanguard StaticMesh parsing code is adapted from
or informed by UE Viewer source references:

- `UEViewer/Unreal/UnObject.cpp`
- `UEViewer/Unreal/UnrealMesh/UnMesh2.cpp`
- `UEViewer/Unreal/UnrealMesh/UnMesh2.h`

UE Viewer is licensed under the MIT License.

Copyright 2022, Konstantin Nosov

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## BunnyTrack.net UT Package Format Guide

`scripts/extractors/extract_bsp.py` cites the BunnyTrack.net Unreal Tournament
package format guide as a background reference for package structure. No
UTPackage.js code is included in this repository.

## Unreal-Library

Unreal-Library is an optional external helper for decompiling UE2 object text
used by a small part of the pipeline. It is not vendored in this repository.
`python vanguard.py fetch-unreal-library` clones it into `external/`, which is
ignored by git; the fetched project retains its own license and notices.

## Spt2Fbx and SpeedTreeRT

`scripts/speedtree/generate_speedtree_runtime_leaf_cards.py` can invoke the
external open-source [VenoMKO/Spt2Fbx](https://github.com/VenoMKO/Spt2Fbx)
bridge to ask a compatible SpeedTree RT 4.x runtime for the leaf-card geometry
stored in Vanguard's embedded `.spt` payloads. Neither `Spt2Fbx.exe` nor the
proprietary `SpeedTreeRT.dll` is copied, linked, or distributed by this
repository. Users supply both files locally, and those files remain governed
by their respective licenses.
