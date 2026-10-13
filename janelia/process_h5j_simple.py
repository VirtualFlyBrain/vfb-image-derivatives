# Vendored from VirtualFlyBrain/Management Janelia/JaneliaWorkstationAPI/process_h5j_simple.py (master c90cc13) for the Argo janelia-load job;
# keep in step with that copy. The loader (janelia/load.py) uses its decode/warp/write helpers only.
#!/usr/bin/env python3
"""
Complete H5J processing with proper volume alignment to unisex template.
Based on successful testing of fundamental steps.

Speed notes (2026-09): the per-image cost used to be dominated by work that is the same
for every image - the target->source coordinate map (now baked once, see warp_fields.py),
the navis/flybrains setup and the VFB KB template lookup (now once per process), and a
per-frame TIFF round trip in the HEVC decode (now a raw pipe). Output files are unchanged.
"""

import h5py
import numpy as np
import tempfile
import subprocess
import os
import argparse
import functools
import json
import re
import nrrd

import warp_fields

KB_ENDPOINT = os.environ.get('KB_ENDPOINT', 'http://kb.virtualflybrain.org')

_transforms_ready = False


def ensure_transforms():
    """Download (if absent) and register the JRC brain + VNC transforms, once per process.

    Only needed when a warp field has to be baked; `warp_volume` itself never touches navis.
    """
    global _transforms_ready
    if _transforms_ready:
        return
    import flybrains
    flybrains.download_jrc_transforms()
    flybrains.download_jrc_vnc_transforms()
    flybrains.register_transforms()
    _transforms_ready = True


def _ffprobe_pix_fmt(hevc_file):
    """(pix_fmt, width, height) of the first video stream, or (None, None, None)."""
    try:
        res = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=pix_fmt,width,height', '-of', 'json', hevc_file],
            check=True, capture_output=True, text=True)
        st = json.loads(res.stdout)['streams'][0]
        return st.get('pix_fmt'), int(st['width']), int(st['height'])
    except Exception as e:  # noqa: BLE001 - fall back to the TIFF path on any probe failure
        print(f"ffprobe failed ({e}); falling back to TIFF decode")
        return None, None, None


def _bits_from_pix_fmt(pix_fmt):
    """Bit depth encoded in an ffmpeg pix_fmt name: gray -> 8, gray12le -> 12, yuv420p10le -> 10."""
    m = re.search(r'(?:^gray|p)(\d+)', pix_fmt)
    return int(m.group(1)) if m else 8


def decode_channel_to_stack_tiff(channel_data, frames, height, width):
    """Original decoder: HEVC -> per-frame TIFFs on disk -> numpy. Kept as the fallback."""
    from skimage import io
    with tempfile.NamedTemporaryFile(suffix='.hevc', delete=False) as tmp:
        tmp.write(channel_data)
        hevc_file = tmp.name

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tiff_pattern = os.path.join(tmpdir, 'frame_%04d.tif')
            cmd = [
                'ffmpeg', '-i', hevc_file,
                '-f', 'image2', tiff_pattern
            ]
            subprocess.run(cmd, check=True, capture_output=True)

            import glob
            tiff_files = sorted(glob.glob(os.path.join(tmpdir, 'frame_*.tif')))

            # Load stack
            first = io.imread(tiff_files[0])
            stack = np.zeros((len(tiff_files),) + first.shape, dtype=first.dtype)
            stack[0] = first
            for i, tf in enumerate(tiff_files[1:], 1):
                stack[i] = io.imread(tf)

            return stack

    finally:
        os.unlink(hevc_file)


def decode_channel_to_stack(channel_data, frames, height, width):
    """Decode HEVC channel data to 3D numpy array in (Z, Y, X) order.

    Streams raw frames out of ffmpeg instead of writing one TIFF per frame and reading
    them back (1.2 GB of disk churn per channel on a 63x brain). The pixel format is
    chosen from the stream's bit depth exactly as ffmpeg's TIFF muxer would have chosen
    it (8-bit -> gray, deeper -> gray16le), so the values are the same as before.
    `H5J_DECODE=tiff` forces the old path.
    """
    if os.environ.get('H5J_DECODE', '').lower() == 'tiff':
        return decode_channel_to_stack_tiff(channel_data, frames, height, width)

    with tempfile.NamedTemporaryFile(suffix='.hevc', delete=False) as tmp:
        tmp.write(channel_data)
        hevc_file = tmp.name
    try:
        pix_fmt, w, h = _ffprobe_pix_fmt(hevc_file)
        if pix_fmt is None:
            return decode_channel_to_stack_tiff(channel_data, frames, height, width)
        bits = _bits_from_pix_fmt(pix_fmt)
        out_fmt, dtype = ('gray', np.uint8) if bits <= 8 else ('gray16le', np.uint16)
        cmd = ['ffmpeg', '-v', 'error', '-i', hevc_file,
               '-f', 'rawvideo', '-pix_fmt', out_fmt, '-']
        res = subprocess.run(cmd, check=True, capture_output=True)
        frame_bytes = w * h * np.dtype(dtype).itemsize
        n = len(res.stdout) // frame_bytes
        if n == 0 or len(res.stdout) != n * frame_bytes:
            raise RuntimeError(f'ffmpeg produced {len(res.stdout)} bytes, not a whole number '
                               f'of {w}x{h} {out_fmt} frames')
        stack = np.frombuffer(res.stdout, dtype=dtype).reshape(n, h, w).copy()
        print(f"Decoded {pix_fmt} stream as {out_fmt}: {n} frames of {h}x{w}")
        return stack
    finally:
        os.unlink(hevc_file)


@functools.lru_cache(maxsize=None)
def get_vfb_template_specs(template_name):
    """
    Query VFB KB for template specifications (dimensions, voxel size).
    Returns dict with 'dims', 'voxdims' and 'short_form' (the template individual's VFB id,
    e.g. VFB_00101567 - the last path element of an image folder registered to it).
    Cached: one query per template per process.
    """
    from vfb_connect.cross_server_tools import VfbConnect

    # Connect to VFB KB
    vc = VfbConnect(neo_endpoint=KB_ENDPOINT)

    try:
        # Query for template specs - return the entire relationship
        query = f"""
        MATCH (t:Template)<-[:depicts]-(tc:Template)-[r:in_register_with]->(tc)
        WHERE '{template_name}' IN t.symbol
        RETURN r, t.short_form
        """

        results = vc.nc.commit_list([query])

        if results and results[0]['data']:
            record = results[0]['data'][0]['row']  # Get first result
            r_props = record[0]  # The relationship properties
            short_form = record[1]

            # Extract extent and voxel from relationship properties
            extent = r_props.get('extent')
            voxel = r_props.get('voxel')

            if extent and voxel:
                # Parse extent and voxel (they're stored as strings like '{"X":1211,"Y":567,"Z":175}')
                import ast
                extent_dict = ast.literal_eval(extent[0])  # Take first element
                voxel_dict = ast.literal_eval(voxel[0])    # Take first element

                # More principled approach: extent represents boundaries in physical units,
                # so dimensions = extent - voxel_size (rounded appropriately)
                # This replaces the hardcoded VNC vs non-VNC logic with a unified calculation
                dims = [
                    round(extent_dict['X'] - voxel_dict['X']),
                    round(extent_dict['Y'] - voxel_dict['Y']),
                    round(extent_dict['Z'] - voxel_dict['Z'])
                ]
                voxdims = [voxel_dict['X'], voxel_dict['Y'], voxel_dict['Z']]

                return {'dims': dims, 'voxdims': voxdims, 'short_form': short_form}

    except Exception as e:
        print(f"Error querying VFB KB for {template_name}: {e}")

    return None


def target_template_specs(target_template):
    """dims/voxdims for the target grid: VFB KB first, flybrains fallback (as before)."""
    vfb_specs = get_vfb_template_specs(target_template)
    if vfb_specs:
        print(f"Using VFB KB specs for {target_template}: dims={vfb_specs['dims']}, voxdims={vfb_specs['voxdims']}")
        return vfb_specs['dims'], vfb_specs['voxdims'], 'VFB KB'
    import flybrains
    target_temp = getattr(flybrains, target_template)
    print(f"Using flybrains fallback for {target_template}: dims={target_temp.dims}, voxdims={target_temp.voxdims}")
    return list(target_temp.dims), list(target_temp.voxdims), 'flybrains'


_field_cache = {}


def get_warp_field(source_template, target_template, validate=False):
    """The baked target->source map for this pair, building it on first use.

    `validate=True` looks the target grid up (VFB KB, flybrains fallback) and checks the
    map on disk was built for it - done once per job by jenkins_pipeline before the
    workers start. Without it an existing map is trusted as-is, so the per-image path
    needs no KB round trip at all.
    """
    key = (source_template, target_template)
    if key in _field_cache and not validate:
        return _field_cache[key]
    npy_path, _ = warp_fields.field_paths(source_template, target_template)
    if validate or not os.path.exists(npy_path):
        dims, voxdims, _ = target_template_specs(target_template)
        if not os.path.exists(npy_path):
            print(f"Warp field {npy_path} not found; baking it now (one-off, several minutes)")
            ensure_transforms()
        field = warp_fields.get_field(source_template, target_template, dims, voxdims)
    else:
        field = warp_fields.WarpField(npy_path)
        if field.source != source_template or field.target != target_template:
            raise ValueError(f'{npy_path}: sidecar says {field.target} -> {field.source}, '
                             f'expected {target_template} -> {source_template}')
    _field_cache[key] = field
    return field


def header_voxdims(source_template, target_template):
    """Voxel size for the output NRRD header: from the warp field when one is used
    (its grid is the target grid), else from the template spec."""
    if source_template != target_template:
        return get_warp_field(source_template, target_template).voxdims
    return target_template_specs(target_template)[1]


def align_volume_to_template(volume_zxy, source_template, target_template, voxel_size_xyz):
    """
    Align volume from source template space to target template space.
    Uses VFB KB specifications for target template. The target->source coordinate map is
    computed once per (source, target) pair by warp_fields and reused for every image.
    """
    field = get_warp_field(source_template, target_template)
    print(f"Warping with {field}")
    aligned_volume = warp_fields.warp_volume(volume_zxy, field, voxel_size_xyz)
    print(f"Final aligned volume shape: {aligned_volume.shape}")
    return aligned_volume


def nrrd_header(target_template, source_template=None):
    """NRRD header for an aligned volume - same fields as before."""
    if source_template is not None:
        voxdims = header_voxdims(source_template, target_template)
    else:
        voxdims = target_template_specs(target_template)[1]
    print(f"NRRD header voxdims={list(voxdims)}")
    return {
        'space directions': np.diag(voxdims),
        'space origin': [0, 0, 0],
        'kinds': ['domain', 'domain', 'domain'],
        'space dimension': 3,
        'space units': ['microns', 'microns', 'microns'],
        'encoding': 'gzip'
    }


def nrrd_compression_level():
    """gzip level for every NRRD written. Default 6 (zlib's default): pynrrd's 9 took
    2.2x longer for a <1% smaller file on real-shaped volumes. NRRD_COMPRESSION_LEVEL
    overrides it for the whole job; the decompressed bytes are identical at any level."""
    return int(os.environ.get('NRRD_COMPRESSION_LEVEL', '6'))


def write_nrrd(path, data, header):
    nrrd.write(path, data, header=header, compression_level=nrrd_compression_level())
    try:
        os.chmod(path, 0o777)
    except PermissionError:
        print(f"Warning: Could not set permissions on {path}")


def to_uint8(stack):
    """uint16 -> uint8 with the same arithmetic as before, one slice at a time.

    The old one-liner materialised the whole stack as float32 (2.4 GB for a 63x brain)
    before casting; slice-wise keeps the peak at one frame.
    """
    if stack.dtype != np.uint16:
        return stack
    out = np.empty(stack.shape, dtype=np.uint8)
    for i in range(stack.shape[0]):
        out[i] = (stack[i].astype(np.float32) / 65535.0 * 255).astype(np.uint8)
    return out


def read_h5j_info(h5j_path):
    """Cheap metadata read: templates and channel layout, without decoding anything."""
    with h5py.File(h5j_path, 'r') as f:
        voxel_size = f.attrs['voxel_size']  # [X, Y, Z]
        alignment_space = f.attrs['alignment_space'].decode()
        is_vnc = 'VNC' in alignment_space
        if 'FEMALE' in alignment_space:
            source_template = 'JRCVNC2018F' if is_vnc else 'JRC2018F'
        elif 'MALE' in alignment_space:
            source_template = 'JRCVNC2018M' if is_vnc else 'JRC2018M'
        else:
            source_template = 'JRCVNC2018U' if is_vnc else 'JRC2018U'
        target_template = 'JRCVNC2018U' if is_vnc else 'JRC2018U'
        channels = f['/Channels']
        signal_channels, reference_channels = [], []
        for key in sorted(channels.keys()):
            content_type = channels[key].attrs['content_type'].decode()
            if content_type == 'signal':
                signal_channels.append(key)
            elif content_type == 'reference':
                reference_channels.append(key)
        return dict(voxel_size=np.asarray(voxel_size, dtype=float),
                    alignment_space=alignment_space,
                    source_template=source_template, target_template=target_template,
                    signal_channels=signal_channels, reference_channels=reference_channels,
                    frames=int(channels.attrs['frames'][0]),
                    height=int(channels.attrs['height'][0]),
                    width=int(channels.attrs['width'][0]))


def process_h5j_file(h5j_path, process_reference=True, process_signal=True, output_dir=None,
                     signal_subset=None, write=True):
    """Process H5J file with proper axis handling and alignment.

    `signal_subset`: optional list of signal channel keys to process (others are skipped,
    but keep their `signal_N` numbering). `write=False` returns the aligned arrays without
    writing NRRDs; the returned dict maps channel name -> (X, Y, Z) array either way.
    """

    print(f"Processing: {os.path.basename(h5j_path)}")

    info = read_h5j_info(h5j_path)
    voxel_size = info['voxel_size']
    alignment_space = info['alignment_space']
    source_template, target_template = info['source_template'], info['target_template']
    signal_channels, reference_channels = info['signal_channels'], info['reference_channels']
    frames, height, width = info['frames'], info['height'], info['width']

    print(f"Alignment space: {alignment_space}")
    print(f"Voxel size [X,Y,Z]: {voxel_size}")
    print(f"Source template: {source_template}")
    print(f"Target template: {target_template}")
    print(f"Dimensions: {frames}×{height}×{width} (Z×Y×X)")

    # Voxel size is already [X, Y, Z]
    voxel_size_xyz = voxel_size

    channels_to_process = []
    if process_reference:
        channels_to_process.extend(reference_channels)
    if process_signal:
        for key in signal_channels:
            if signal_subset is None or key in signal_subset:
                channels_to_process.append(key)

    print(f"Processing {len(channels_to_process)} channels: {channels_to_process}")

    header = nrrd_header(target_template, source_template) if write else None
    processed_volumes = {}

    with h5py.File(h5j_path, 'r') as f:
        channels = f['/Channels']
        for ch_key in channels_to_process:
            print(f"\nProcessing channel: {ch_key}")

            channel_data = channels[ch_key][()]
            stack_zxy = decode_channel_to_stack(channel_data, frames, height, width)
            del channel_data

            print(f"Decoded shape (Z,Y,X): {stack_zxy.shape}")

            # Convert data type if needed
            stack_zxy = to_uint8(stack_zxy)

            # Apply threshold clipping to signal channels only
            if ch_key in signal_channels:
                threshold = int(os.environ.get('SIGNAL_THRESHOLD', '0'))
                if threshold > 0:
                    print(f"Applying signal threshold: {threshold} (values 0-{threshold-1} set to 0)")
                    stack_zxy[stack_zxy < threshold] = 0

            # Align to target template
            if source_template != target_template:
                print("Aligning to target template...")
                aligned_xyz = align_volume_to_template(stack_zxy, source_template, target_template, voxel_size_xyz)
                print(f"Aligned shape: {aligned_xyz.shape}")
            else:
                # Transpose from (Z, Y, X) to (X, Y, Z) for saving
                aligned_xyz = np.ascontiguousarray(stack_zxy.transpose(2, 1, 0))
                print("No alignment needed, transposed to XYZ")
            del stack_zxy

            # Same numbering as before: reference first, then signals in sorted order
            if ch_key in reference_channels:
                channel_name = 'reference'
            else:
                offset = len(reference_channels) if process_reference else 0
                channel_name = f'signal_{offset + signal_channels.index(ch_key)}'

            if write:
                base_path = os.path.splitext(h5j_path)[0]
                if output_dir:
                    output_dir_abs = os.path.abspath(output_dir)
                    os.makedirs(output_dir_abs, exist_ok=True)
                    nrrd_path = os.path.join(output_dir_abs, f"{os.path.basename(base_path)}_{channel_name}.nrrd")
                else:
                    nrrd_path = f"{base_path}_{channel_name}.nrrd"
                write_nrrd(nrrd_path, aligned_xyz, header)
                print(f"Saved: {nrrd_path}")

            processed_volumes[channel_name] = aligned_xyz

    return processed_volumes


def main():
    print("Starting H5J processing script...")
    parser = argparse.ArgumentParser(description="Process H5J file with proper alignment")
    parser.add_argument('h5j_path', help='Path to H5J file')
    parser.add_argument('--reference-only', action='store_true', help='Process only reference channel')
    parser.add_argument('--signal-only', action='store_true', help='Process only signal channels')
    parser.add_argument('--output-dir', default=None, help='Directory to save aligned NRRD files')

    args = parser.parse_args()
    print(f"Parsed arguments: h5j_path={args.h5j_path}, reference_only={args.reference_only}, signal_only={args.signal_only}, output_dir={args.output_dir}")

    # Validate arguments
    if args.reference_only and args.signal_only:
        print("Error: Cannot specify both --reference-only and --signal-only")
        return

    try:
        print("Starting file processing...")
        volumes = process_h5j_file(args.h5j_path,
                                process_reference=not args.signal_only,
                                process_signal=not args.reference_only,
                                output_dir=args.output_dir)
        print(f"\nSuccessfully processed {len(volumes)} channels")
    except Exception as e:
        print(f"Error processing file: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
