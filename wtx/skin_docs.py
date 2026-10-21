"""skin_docs.py - the text file that goes into a skin template folder.

  INSTRUCTIONS.txt  how a custom skin works: what the .blk may and may not contain, the alpha rule, common fixes
"""
from __future__ import annotations

from wtx.uv_guide import GUIDE_DIR
from wtx.skin_details import C_DETAIL_FORMAT, DETAILS_DIR


def instructions_text(vehicle_name: str, vehicle_id: str, ext: str = ".tga") -> str:
    return f"""CUSTOM SKIN - {vehicle_name} ({vehicle_id})
Made by WT Asset Explorer.

WHAT IS IN THIS FOLDER
  {vehicle_id}.blk   tells the game which of its textures to swap for which file of this folder
  *{ext}            the textures to paint. Their names must not change
  {GUIDE_DIR}/  one PNG per colour (_c) texture: the outline of each UV island, i.e. where each part of the model sits on the texture.
                     Use it as a layer on top while painting; do not save it into the texture
  {DETAILS_DIR}/  the vehicle's panel lines, rivets and weld seams as separate layers, to paste over your own paint (see DETAILS below)
  INSTRUCTIONS.txt   this file

HOW TO USE IT
  1. Paint the {ext} files in Photoshop, GIMP, Substance, Blender... Keep the size and the file name, save as a
     32-bit TGA (with alpha) and do not flip or rotate the picture.
  2. To see your painting on the model before starting the game: in the app press "Preview custom skin...", pick
     this folder, and you get an OBJ or GLB (the format chosen in the app) with your textures on it.
  3. Copy this whole folder into War Thunder/UserSkins, then pick it under "User skin" in the vehicle's
     customization screen.

DETAILS ({DETAILS_DIR}/, rebuilt on every export; they are layers, not files the game reads)
  <name>_c.{C_DETAIL_FORMAT}   grey layer with transparency: only the panel lines and details (grooves dark, rivet edges light), flat areas have no data;
                 computed from the normal map of the same material, so it holds none of the game's paint.
                 Open it in your editor and put it above your paint (Normal mode, or try Overlay / Soft Light); the background stays see-through.
                 Same size and layout as the _c texture of the same name. Materials without a normal map get no file.
  <name>_n.tga   the normal map in the game's layout (X in alpha, Y in green; red and blue are 0, they hold no gloss or metal).
                 Paste only the alpha and green channels over your own normal map.

THE .BLK
  - The very first line has to be exactly:   name:t="user"
    Nothing may be above it, not even a comment, or the skin is not read.
  - After it comes one block per texture:
        replace_tex {{
          from:t="game_texture_name*"
          to:t="your_file{ext}"
        }}
    "from" is the game's own texture name with a * at the end: do not change it.
    "to" is the file in this folder: the name AND the extension have to match exactly.
  - Use {ext} files. The .dds files this tool wrote first were ignored by the game when tested; TGA was picked up.
  - If you rename a file, change the "to" line too.
  - The tool never overwrites a .blk that exists: a new one is written as {vehicle_id}.blk.new next to it.

THE ALPHA CHANNEL (tanks and ships)
  Sometimes the game still paints its own camouflage on top of a custom skin. If that happens, open that colour
  texture, fill its alpha channel completely with white (no black or grey left in it) and save it as a 32-bit TGA
  with the alpha kept. That normally fixes it.
  The camo the vehicle normally wears is already baked into the texture as a starting point.

COMMON FIXES
  The skin does not show at all
    - First line of the .blk is not name:t="user" (a comment or blank line above it).
    - The .blk is not called {vehicle_id}.blk, or the folder is not inside War Thunder/UserSkins.
    - A "to" file is missing, misspelled, or has another extension (renaming .png to .tga does not convert it).
    - The skin is not selected under "User skin" for this vehicle.
  Camo or the normal paint is still on top
    - Fill the alpha channel of that texture completely with white and save the TGA with its alpha (see above).
  One part did not change
    - It uses a different texture. Turret, skirts, gun and so on are usually separate files: look at each .tga and its uv guide.
    - Only the colour (_c) and normal (_n) textures are written. Shared detail and effect textures are left out
      on purpose.
  The painting is in the wrong place
    - Compare with {GUIDE_DIR}/<file>_uv.png. The picture must keep its original size and orientation.
  Strange lighting or shiny / dented look
    - The _n file (normal map) was edited with colour. Delete it and run "Create skin template" again to get the
      original back (existing files are never overwritten).
  The preview looks different from the game
    - The preview shows the plain texture under Blender's lighting: no game shaders, no camo mask, no dirt layer.
      It is for checking the painting, not for judging the final look.
"""
