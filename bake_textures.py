import os
import subprocess
import sys
from PIL import Image

def bake_textures(export_dir):
    textures_dir = os.path.join(export_dir, "textures")
    if not os.path.exists(textures_dir):
        print(f"[ERROR] Textures directory not found at: {textures_dir}")
        return

    # Locate texconv.exe in the current or parent directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    texconv_path = os.path.join(script_dir, "texconv.exe")
    if not os.path.exists(texconv_path):
        texconv_path = "texconv.exe" # Fallback to system path execution

    # Find the camouflage texture inside the textures folder
    camo_files = [f for f in os.listdir(textures_dir) if "camo" in f.lower() and f.lower().endswith('.dds')]
    if not camo_files:
        print("[WARNING] No camouflage texture found in the textures folder to bake!")
        return
    
    camo_dds = os.path.join(textures_dir, camo_files[0])
    print(f"[INFO] Using camouflage file: {camo_files[0]}")

    # Convert camo DDS to PNG so Pillow can read it
    subprocess.run([texconv_path, "-y", "-ft", "png", "-o", textures_dir, camo_dds], stdout=subprocess.DEVNULL)
    camo_png = os.path.splitext(camo_dds)[0] + ".png"
    camo_img = Image.open(camo_png).convert("RGB")

    # Find all base color textures ending with _c.dds
    base_textures = [f for f in os.listdir(textures_dir) if f.endswith('_c.dds') and not '_baked' in f]
    baked_mapping = {}

    for base_f in base_textures:
        base_dds = os.path.join(textures_dir, base_f)
        print(f"[INFO] Baking texture: {base_f}")

        # Convert base DDS to PNG
        subprocess.run([texconv_path, "-y", "-ft", "png", "-o", textures_dir, base_dds], stdout=subprocess.DEVNULL)
        base_png = os.path.splitext(base_dds)[0] + ".png"
        
        base_img = Image.open(base_png).convert("RGBA")
        r, g, b, a = base_img.split()
        base_rgb = Image.merge("RGB", (r, g, b))

        # Tile the camouflage pattern to cover the dimensions of the base texture
        w, h = base_img.size
        tiled_camo = Image.new("RGB", (w, h))
        cw, ch = camo_img.size
        for x in range(0, w, cw):
            for y in range(0, h, ch):
                tiled_camo.paste(camo_img, (x, y))

        # Composite the camo pattern over the base texture using the alpha mask
        composite = Image.composite(tiled_camo, base_rgb, a)

        # Save the composite as a temporary PNG, then convert back to DDS via texconv
        baked_png_name = os.path.splitext(base_f)[0] + "_baked.png"
        baked_png_path = os.path.join(textures_dir, baked_png_name)
        composite.save(baked_png_path)

        baked_dds_name = os.path.splitext(base_f)[0] + "_baked.dds"
        baked_dds_path = os.path.join(textures_dir, baked_dds_name)

        subprocess.run([texconv_path, "-y", "-ft", "dds", "-o", textures_dir, baked_png_path], stdout=subprocess.DEVNULL)
        
        # Track original name vs baked name for MTL updating
        baked_mapping[base_f] = baked_dds_name

        # Clean up temporary PNG files
        if os.path.exists(base_png): os.remove(base_png)
        if os.path.exists(baked_png_path): os.remove(baked_png_path)

    if os.path.exists(camo_png): os.remove(camo_png)

    # Automatically update all .mtl files in the export folder to use the baked textures
    for file in os.listdir(export_dir):
        if file.endswith('.mtl'):
            mtl_path = os.path.join(export_dir, file)
            with open(mtl_path, 'r') as f:
                mtl_content = f.read()
            
            for orig_c, baked_c in baked_mapping.items():
                mtl_content = mtl_content.replace(orig_c, baked_c)
            
            with open(mtl_path, 'w') as f:
                f.write(mtl_content)
            print(f"[SUCCESS] Updated MTL configuration file: {file}")

    print("[DONE] All textures successfully baked and ready for Blender!")

if __name__ == "__main__":
    # Pass the export folder path as an argument, or default to current working directory
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    bake_textures(target_dir)