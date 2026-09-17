"""
processor.py
Core GIS processing engine optimized for QGIS 3 with Shapely 2.0 / GeoPandas.
Targeted buffering, STRtree indexing, multi-threaded parallel execution.
"""

import os
import numpy as np
import geopandas as gpd
import pandas as pd
import shapely
import shapely.affinity
from shapely.geometry import shape, MultiPolygon, Polygon, box
from shapely.ops import unary_union
import warnings
import concurrent.futures

warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", message=".*Geometry is in a geographic CRS.*")

try:
    import pyogrio
    ENGINE = "pyogrio"
    HAS_PYOGRIO = True
except ImportError:
    ENGINE = "fiona"
    HAS_PYOGRIO = False

ROAD_BUFFER_M       = 1
SMALL_AREA_THRESH   = 3
HIGH_OVERLAP_RATIO  = 0.20


def clean_coords(coords):
    if not isinstance(coords, (list, tuple)):
        return coords
    if len(coords) > 0 and isinstance(coords[0], (tuple, list)) and isinstance(coords[0][0], (int, float)):
        if coords[0] != coords[-1]:
            return list(coords) + [coords[0]]
        return coords
    return [clean_coords(c) for c in coords]


def robust_load_shp(path, log_callback):
    filename = os.path.basename(path)
    try:
        if HAS_PYOGRIO:
            gdf = pyogrio.read_dataframe(path)
        else:
            gdf = gpd.read_file(path, engine="fiona")
            
        invalid_mask = ~gdf.geometry.is_valid
        num_invalid = invalid_mask.sum()
        if num_invalid > 0:
            log_callback(f"  -> Found {num_invalid} invalid geometries in {filename}. Repairing...")
            gdf["geometry"] = shapely.make_valid(gdf.geometry.values)
            
        return gdf
    except Exception as e:
        log_callback(f"  -> Direct read failed: {e}. Initiating manual geometry recovery...")
        import fiona
        data = []
        try:
            with fiona.open(path, 'r') as source:
                crs = source.crs
                for feature in source:
                    geom_dict = feature['geometry']
                    properties = dict(feature['properties'])
                    if geom_dict is None:
                        continue
                    try:
                        geom = shape(geom_dict)
                    except Exception:
                        try:
                            geom_dict['coordinates'] = clean_coords(geom_dict['coordinates'])
                            geom = shape(geom_dict)
                        except Exception:
                            continue 

                    if not geom.is_valid:
                        geom = shapely.make_valid(geom)

                    properties['geometry'] = geom
                    data.append(properties)
            
            if not data:
                raise ValueError(f"No valid geometries recovered from {filename}.")
            
            return gpd.GeoDataFrame(data, crs=crs)
        except Exception as e2:
            raise RuntimeError(f"Could not load {filename}:\n{e2}")


def extract_polygons(geom):
    if geom is None or geom.is_empty: return None
    if isinstance(geom, (Polygon, MultiPolygon)): return geom
    if geom.geom_type == 'GeometryCollection' or hasattr(geom, 'geoms'):
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        return unary_union(polys) if polys else None
    return None


def largest_polygon(geom):
    geom = extract_polygons(geom)
    if geom is None or geom.is_empty: return None
    if isinstance(geom, Polygon): return geom
    if isinstance(geom, MultiPolygon): return max(geom.geoms, key=lambda g: g.area)
    return None


def build_patches_fast(movable_polys):
    if movable_polys.empty:
        return []
    
    geoms = movable_polys.geometry.values
    idx_map = movable_polys.index.values
    tree = shapely.STRtree(geoms)
    
    left, right = tree.query(geoms, predicate="intersects")
    
    n = len(geoms)
    adj = [[] for _ in range(n)]
    for l, r in zip(left, right):
        if l != r:
            adj[l].append(r)
            
    visited = np.zeros(n, dtype=bool)
    patches = []
    
    for i in range(n):
        if not visited[i]:
            queue = [i]
            visited[i] = True
            head = 0
            while head < len(queue):
                curr = queue[head]
                head += 1
                for neighbor in adj[curr]:
                    if not visited[neighbor]:
                        visited[neighbor] = True
                        queue.append(neighbor)
            patches.append([idx_map[x] for x in queue])
            
    return patches


def get_compactness(geom):
    if geom is None or geom.is_empty or geom.length == 0: 
        return 0
    return (4 * np.pi * geom.area) / (geom.length ** 2)


def is_shape_preserved(orig_geom, new_geom):
    if not new_geom or new_geom.is_empty or not new_geom.is_valid:
        return False
    if new_geom.area < SMALL_AREA_THRESH:
        return False
    if new_geom.area < 0.75 * orig_geom.area:
        return False
        
    orig_c = get_compactness(orig_geom)
    new_c = get_compactness(new_geom)
    if orig_c > 0:
        if new_c < 0.5 * orig_c or new_c > 2.0 * orig_c:
            return False
            
    return True


def check_width_uniformity(geom, tolerance=0.10):
    if geom is None or geom.is_empty:
        return True
        
    mrr = geom.minimum_rotated_rectangle
    if mrr.geom_type != 'Polygon':
        return True
        
    coords = list(mrr.exterior.coords)
    if len(coords) < 5:
        return True
        
    c0, c1, c2 = coords[0], coords[1], coords[2]
    d01 = np.hypot(c1[0] - c0[0], c1[1] - c0[1])
    d12 = np.hypot(c2[0] - c1[0], c2[1] - c1[1])
    
    if d01 >= d12:
        length_vec = np.array(c1) - np.array(c0)
        width_vec = np.array(c2) - np.array(c1)
        start_pt = np.array(c0)
    else:
        length_vec = np.array(c2) - np.array(c1)
        width_vec = np.array(c1) - np.array(c0)
        start_pt = np.array(c1)
        
    sample_ratios = [0.20, 0.35, 0.50, 0.65, 0.80]
    widths = []
    
    for r in sample_ratios:
        p_base = start_pt + r * length_vec
        p_start = p_base - 0.1 * width_vec
        p_end = p_base + 1.1 * width_vec
        test_line = shapely.geometry.LineString([p_start, p_end])
        try:
            inter = geom.intersection(test_line)
            if inter is not None and not inter.is_empty:
                widths.append(inter.length)
        except Exception:
            pass
            
    if len(widths) < 3:
        return False
        
    min_w = min(widths)
    max_w = max(widths)
    avg_w = sum(widths) / len(widths)
    if avg_w == 0:
        return True
        
    return ((max_w - min_w) / avg_w) <= tolerance


def should_keep_trimmed_part(geom, parent_geom):
    if geom is None or geom.is_empty or parent_geom is None or parent_geom.is_empty:
        return False
    if geom.area <= 1.0:
        return False
        
    mrr = geom.minimum_rotated_rectangle
    if mrr.geom_type == 'Polygon':
        coords = list(mrr.exterior.coords)
        p0, p1, p2 = coords[0], coords[1], coords[2]
        d1 = np.hypot(p1[0] - p0[0], p1[1] - p0[1])
        d2 = np.hypot(p2[0] - p1[0], p2[1] - p1[1])
        width = min(d1, d2)
    else:
        width = 0
    if width <= 2.0:
        return False

    rectangularity = (geom.area / mrr.area) if mrr.area > 0 else 0
        
    try:
        if isinstance(geom, Polygon):
            num_vertices = len(set(geom.exterior.coords[:-1]))
        elif isinstance(geom, MultiPolygon):
            num_vertices = sum(len(set(g.exterior.coords[:-1])) for g in geom.geoms)
        else:
            num_vertices = 0
    except Exception:
        num_vertices = 0
        
    is_triangle_like = (rectangularity <= 0.72) or (num_vertices == 3)
    if not is_triangle_like:
        return False
        
    if geom.area < 0.10 * parent_geom.area:
        return False
        
    if check_width_uniformity(geom, tolerance=0.10):
        return False
        
    return True


def is_move_valid_fast(patch_union, dx, dy, local_roads_union, local_other_union):
    shifted_union = shapely.affinity.translate(patch_union, xoff=dx, yoff=dy)
    if not shifted_union.is_valid:
        return False
    if local_roads_union is not None and not local_roads_union.is_empty:
        if shifted_union.intersects(local_roads_union):
            if shifted_union.intersection(local_roads_union).area > 0.01:
                return False
    if local_other_union is not None and not local_other_union.is_empty:
        if shifted_union.intersects(local_other_union):
            if shifted_union.intersection(local_other_union).area > 0.01:
                return False
    return True


def process_patch_task(
    patch_indices,
    poly_to_roads,
    polys_geom_dict,
    buffered_roads_dict,
    retained_polys_geoms,
    retained_tree,
    road_tree_buffered,
    buffered_roads_geoms,
    retained_idx_map,
    enable_trim,
    enable_move
):
    overlapping_polys = [i for i in patch_indices if i in poly_to_roads]
    results = {}
    
    if not overlapping_polys:
        for i in patch_indices:
            results[i] = (polys_geom_dict[i], "unchanged")
        return results, 0
        
    poly_overlap_info = {}
    for i in overlapping_polys:
        geom = polys_geom_dict[i]
        r_geoms = [buffered_roads_dict[r] for r in poly_to_roads[i] if r in buffered_roads_dict]
        if not r_geoms:
            continue
        local_r_union = shapely.ops.unary_union(r_geoms)
        if not local_r_union.is_valid:
            local_r_union = shapely.make_valid(local_r_union)
            
        inter = geom.intersection(local_r_union)
        if inter.area > 0.01:
            ratio = inter.area / geom.area
            poly_overlap_info[i] = {'ratio': ratio, 'road_union': local_r_union}
            
    if not poly_overlap_info:
        for i in patch_indices:
            results[i] = (polys_geom_dict[i], "unchanged")
        return results, 0
        
    num_overlaps = len(poly_overlap_info)
    is_patch = len(patch_indices) > 1
    patch_needs_move = is_patch and num_overlaps > 3 and enable_move
    move_successful = False
    
    patch_geom_dict = {i: polys_geom_dict[i] for i in patch_indices}
    patch_union = shapely.ops.unary_union(list(patch_geom_dict.values()))
    if not patch_union.is_valid:
        patch_union = shapely.make_valid(patch_union)
        
    # Attempt patch shift
    if patch_needs_move:
        search_bounds = patch_union.buffer(10).bounds
        query_box = box(*search_bounds)
        
        local_road_idx = road_tree_buffered.query(query_box, predicate="intersects")
        r_union = shapely.ops.unary_union(buffered_roads_geoms[local_road_idx]) if len(local_road_idx) > 0 else None
            
        local_other_idx = retained_tree.query(query_box, predicate="intersects")
        local_other_geoms = [
            retained_polys_geoms[idx]
            for idx in local_other_idx
            if retained_idx_map[idx] not in patch_indices
        ]
        o_union = shapely.ops.unary_union(local_other_geoms) if local_other_geoms else None
        
        dx, dy = None, None
        angles = np.deg2rad(np.arange(0, 360, 22.5))
        for dist in np.arange(0.5, 6.5, 0.5):
            for angle in angles:
                tdx = dist * np.cos(angle)
                tdy = dist * np.sin(angle)
                if is_move_valid_fast(patch_union, tdx, tdy, r_union, o_union):
                    dx, dy = tdx, tdy
                    break
            if dx is not None:
                break
                
        if dx is not None:
            move_successful = True
            for i, geom in patch_geom_dict.items():
                results[i] = (shapely.affinity.translate(geom, xoff=dx, yoff=dy), "moved")
                
    # Fallback to trimming
    if not move_successful:
        patch_boundary = None
        if is_patch:
            try:
                patch_boundary = patch_union.boundary
            except Exception:
                patch_boundary = None
                
        for i in patch_indices:
            if i in poly_overlap_info:
                info = poly_overlap_info[i]
                geom = polys_geom_dict[i]
                eligible_for_edit = False
                if not is_patch:
                    eligible_for_edit = True
                else:
                    if patch_boundary is not None:
                        try:
                            if geom.boundary.intersects(patch_boundary):
                                eligible_for_edit = True
                        except Exception:
                            pass
                            
                if eligible_for_edit and info['ratio'] < HIGH_OVERLAP_RATIO and enable_trim:
                    try:
                        trimmed = geom.difference(info['road_union'])
                        trimmed = extract_polygons(trimmed)
                        if trimmed and not trimmed.is_empty:
                            largest = largest_polygon(trimmed)
                            if is_shape_preserved(geom, largest):
                                results[i] = (largest, "edited")
                            else:
                                results[i] = (geom, "unresolved")
                        else:
                            results[i] = (geom, "unresolved")
                    except Exception:
                        results[i] = (geom, "unresolved")
                else:
                    results[i] = (geom, "unresolved")
            else:
                results[i] = (polys_geom_dict[i], "unchanged")
                
    return results, num_overlaps


def run_correction(
    road_path: str,
    poly_path: str,
    output_path: str,
    aoi_path: str = None,
    enable_trim: bool = True,
    enable_delete: bool = True,
    enable_move: bool = True,
    progress_callback=None,
    stats_callback=None,
    log_callback=None,
):
    def log(msg):
        if log_callback: log_callback(msg)

    # 1. Load Polygons
    log("Reading Polygons Shapefile...")
    polys = robust_load_shp(poly_path, log)
    log(f"  -> Found {len(polys):,} polygon records.")
    
    poly_original_crs = polys.crs 
    if polys.crs and polys.crs.is_geographic:
        log("  -> Reprojecting to estimated UTM (Meters) for accurate geometric operations...")
        polys = polys.to_crs(polys.estimate_utm_crs())

    # Pre-clean dangling nodes/spikes
    cleaned_geoms = [extract_polygons(shapely.make_valid(g)) if (g and not g.is_empty) else None for g in polys.geometry.values]
    polys["geometry"] = cleaned_geoms
    polys = polys.dropna(subset=["geometry"]).reset_index(drop=True)

    # 2. AOI Masking
    inside_aoi_mask = pd.Series(False, index=polys.index)
    if aoi_path and os.path.isfile(aoi_path):
        log(f"\nReading AOI Shapefile ({os.path.basename(aoi_path)})...")
        try:
            aoi_gdf = robust_load_shp(aoi_path, log)
            if not aoi_gdf.empty:
                if polys.crs and aoi_gdf.crs != polys.crs:
                    aoi_gdf = aoi_gdf.to_crs(polys.crs)
                
                aoi_union = shapely.ops.unary_union(aoi_gdf.geometry.values)
                if not aoi_union.is_valid:
                    aoi_union = shapely.make_valid(aoi_union)
                
                inside_aoi_mask = polys.geometry.intersects(aoi_union)
                protected_count = inside_aoi_mask.sum()
                log(f"  -> AOI Applied: {protected_count:,} polygons inside AOI will remain UNCHANGED.")
        except Exception as e:
            log(f"  -> WARNING: AOI processing failed: {e}. Proceeding without AOI.")
            inside_aoi_mask = pd.Series(False, index=polys.index)

    # 3. Load Roads
    log("\nReading Roads Shapefile...")
    roads = robust_load_shp(road_path, log)
    roads = roads.dropna(subset=["geometry"]).reset_index(drop=True)
    if roads.crs != polys.crs:
        roads = roads.to_crs(polys.crs)

    # 4. Exclusions & Purging
    has_bldg = pd.Series(False, index=polys.index)
    if 'BLDG_NAME' in polys.columns:
        has_bldg = polys['BLDG_NAME'].notna() & (polys['BLDG_NAME'].astype(str).str.strip() != '')

    protected_polys = has_bldg | inside_aoi_mask
    areas = polys.geometry.area
    to_delete = pd.Series(False, index=polys.index)
    road_geoms = roads.geometry.values
    road_tree = shapely.STRtree(road_geoms) if len(road_geoms) > 0 else None

    if enable_delete and road_tree is not None:
        small_mask = (areas < SMALL_AREA_THRESH) & (~protected_polys)
        if small_mask.any():
            small_indices = polys.index[small_mask].values
            small_geoms = polys.geometry.loc[small_indices].values
            hit_small_pos, _ = road_tree.query(small_geoms, predicate="intersects")
            if len(hit_small_pos) > 0:
                touching_small_indices = small_indices[np.unique(hit_small_pos)]
                to_delete.loc[touching_small_indices] = True
                log(f"  -> Identified {len(touching_small_indices):,} small polygon(s) (<{SMALL_AREA_THRESH}m²) touching roads for purging.")

    to_process_mask = ~(protected_polys | to_delete)
    movable_polys = polys[to_process_mask]

    result_geoms = {}
    statuses = {}
    stats = {"total_overlap": 0, "corrected": 0, "moved": 0, "deleted": 0, "unresolved": 0, "unchanged": 0}

    for i in polys.index:
        if to_delete[i]:
            statuses[i] = "deleted"
            stats["deleted"] += 1
            result_geoms[i] = None 
        elif protected_polys[i]:
            statuses[i] = "unchanged"
            stats["unchanged"] += 1
            result_geoms[i] = polys.geometry.loc[i]

    # 5. Intersections
    log("Querying road-polygon intersections...")
    poly_geoms = movable_polys.geometry.values
    if road_tree is None:
        road_tree = shapely.STRtree(road_geoms)
    
    poly_indices, road_indices = road_tree.query(poly_geoms, predicate="intersects")
    movable_idx_map = movable_polys.index.values
    road_idx_map = roads.index.values
    
    from collections import defaultdict
    poly_to_roads = defaultdict(list)
    intersecting_road_indices = set()
    for p_pos, r_pos in zip(poly_indices, road_indices):
        p_idx = movable_idx_map[p_pos]
        r_idx = road_idx_map[r_pos]
        poly_to_roads[p_idx].append(r_idx)
        intersecting_road_indices.add(r_idx)

    # 6. Targeted Road Buffers
    log(f"Buffering {len(intersecting_road_indices):,} intersecting road lines by {ROAD_BUFFER_M}m...")
    buffered_roads_dict = {}
    for r_idx in intersecting_road_indices:
        buffered_roads_dict[r_idx] = roads.geometry.loc[r_idx].buffer(ROAD_BUFFER_M, cap_style=2, join_style=2)

    patches = build_patches_fast(movable_polys)
    retained_polys = polys[~to_delete]
    retained_polys_geoms = retained_polys.geometry.values
    retained_idx_map = retained_polys.index.values
    retained_tree = shapely.STRtree(retained_polys_geoms)
    
    buffered_roads_geoms = np.array(list(buffered_roads_dict.values()))
    road_tree_buffered = shapely.STRtree(buffered_roads_geoms)

    log("Resolving overlaps in parallel worker threads...")
    total_patches = len(patches)
    polys_geom_dict = polys.geometry.to_dict()
    max_workers = os.cpu_count() or 4

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                process_patch_task,
                patch_indices,
                poly_to_roads,
                polys_geom_dict,
                buffered_roads_dict,
                retained_polys_geoms,
                retained_tree,
                road_tree_buffered,
                buffered_roads_geoms,
                retained_idx_map,
                enable_trim,
                enable_move
            ): patch_indices
            for patch_indices in patches
        }
        
        for completed_count, future in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                patch_results, num_overlaps = future.result()
                stats["total_overlap"] += num_overlaps
                for idx, (new_geom, status) in patch_results.items():
                    result_geoms[idx] = new_geom
                    statuses[idx] = status
                    if status == "edited": stats["corrected"] += 1
                    elif status == "moved": stats["moved"] += 1
                    elif status == "unresolved": stats["unresolved"] += 1
                    elif status == "unchanged": stats["unchanged"] += 1
            except Exception as exc:
                log(f"Batch processing exception: {exc}")
                
            if progress_callback and (completed_count % 100 == 0 or completed_count == total_patches):
                progress_callback(completed_count, total_patches)
            if stats_callback and (completed_count % 100 == 0 or completed_count == total_patches):
                stats_callback(dict(stats))

    # 7. Construct Output GeoDataFrames
    main_mask = polys.index.map(lambda idx: statuses.get(idx) != "deleted")
    main_gdf = polys[main_mask].copy()
    main_gdf["geometry"] = main_gdf.index.map(result_geoms)
    main_gdf["status"] = main_gdf.index.map(statuses)
    
    trimmed_mask = polys.index.map(lambda idx: statuses.get(idx) == "edited")
    trimmed_parts_gdf = polys[trimmed_mask].copy()
    if not trimmed_parts_gdf.empty:
        trimmed_parts_geoms = []
        for idx in trimmed_parts_gdf.index:
            try:
                diff = polys.geometry.loc[idx].difference(result_geoms[idx])
                diff = extract_polygons(diff)
            except Exception:
                diff = None
            trimmed_parts_geoms.append(diff)
        
        trimmed_parts_gdf["geometry"] = trimmed_parts_geoms
        keep_indices = [idx for idx in trimmed_parts_gdf.index if should_keep_trimmed_part(trimmed_parts_gdf.geometry.loc[idx], polys.geometry.loc[idx])]
        trimmed_parts_gdf = trimmed_parts_gdf.loc[keep_indices]
        trimmed_parts_gdf["status"] = "trimmed_off"
    
    def extract_pts(status_val):
        mask = polys.index.map(lambda idx: statuses.get(idx) == status_val)
        sub = polys[mask].copy()
        if sub.empty: return sub
        if status_val != "deleted":
            sub["geometry"] = sub.index.map(result_geoms)
        sub["geometry"] = sub.geometry.representative_point()
        sub["status"] = status_val
        return sub

    edited_pts = extract_pts("edited")
    moved_pts = extract_pts("moved")
    deleted_pts = extract_pts("deleted")

    # 8. Reproject to Original CRS & Save Files
    if poly_original_crs:
        if not main_gdf.empty: main_gdf = main_gdf.to_crs(poly_original_crs)
        if not edited_pts.empty: edited_pts = edited_pts.to_crs(poly_original_crs)
        if not moved_pts.empty: moved_pts = moved_pts.to_crs(poly_original_crs)
        if not deleted_pts.empty: deleted_pts = deleted_pts.to_crs(poly_original_crs)
        if not trimmed_parts_gdf.empty: trimmed_parts_gdf = trimmed_parts_gdf.to_crs(poly_original_crs)

    base_dir = os.path.dirname(output_path)
    base_name = os.path.splitext(os.path.basename(output_path))[0]
    if base_name.endswith("_polygons"): base_name = base_name[:-9]

    # Save outputs
    saver = lambda df, path: df.to_file(path, engine="pyogrio" if HAS_PYOGRIO else "fiona")
    saver(main_gdf, output_path)

    for df, suffix in [(edited_pts, "edited_pts"), (moved_pts, "moved_pts"), (deleted_pts, "deleted_pts")]:
        if not df.empty:
            saver(df, os.path.join(base_dir, f"{base_name}_{suffix}.shp"))

    if not trimmed_parts_gdf.empty:
        saver(trimmed_parts_gdf, os.path.join(base_dir, f"{base_name}_trimmed_parts.shp"))

    # Generate irregular CSV
    if not trimmed_parts_gdf.empty:
        csv_out_path = os.path.join(base_dir, f"{base_name}_unnecessary_parts.csv")
        bldgid_col = next((c for c in ['BLDGID', 'bldgid', 'BldgID', 'BLDG_NAME', 'bldg_name', 'id', 'ID', 'fid'] if c in polys.columns), None)
        
        csv_records = []
        for idx in trimmed_parts_gdf.index:
            geom = trimmed_parts_gdf.geometry.loc[idx]
            parent_id = polys.loc[idx, bldgid_col] if bldgid_col else idx
            temp_gs = gpd.GeoSeries([geom], crs=polys.crs).to_crs("EPSG:4326") if polys.crs else None
            rep_pt = temp_gs.iloc[0].representative_point() if temp_gs is not None else geom.representative_point()
            csv_records.append({
                "BLDGID": f"{parent_id}_unnecessary",
                "Lat": rep_pt.y,
                "Long": rep_pt.x,
                "BLDGID of Parent polygon": parent_id
            })
        if csv_records:
            pd.DataFrame(csv_records).to_csv(csv_out_path, index=False)

    log("\nAll correction steps completed successfully.")
    return stats