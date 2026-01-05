"""
DXF File Parser for Sheet Metal Quoting
Extracts geometry data: cut length, holes, bends, dimensions
"""
from typing import Optional, List, Tuple
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from pydantic import BaseModel
import math
import io

router = APIRouter()


class DXFAnalysisResult(BaseModel):
    """Result of DXF file analysis"""
    # Bounding box
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    flat_length: float  # max_x - min_x
    flat_width: float   # max_y - min_y
    
    # Geometry counts
    total_cut_length: float  # inches
    num_holes: int
    num_slots: int
    num_bends: int
    num_entities: int
    
    # Detailed breakdown
    hole_diameters: List[float]
    bend_lengths: List[float]
    
    # Layers found
    layers: List[str]
    
    # Warnings
    warnings: List[str]


def calculate_arc_length(start_angle: float, end_angle: float, radius: float) -> float:
    """Calculate arc length given angles in degrees and radius"""
    # Normalize angles
    if end_angle < start_angle:
        end_angle += 360
    angle_span = abs(end_angle - start_angle)
    return 2 * math.pi * radius * (angle_span / 360)


def calculate_polyline_length(points: List[Tuple[float, float]], is_closed: bool = False) -> float:
    """Calculate total length of a polyline"""
    if len(points) < 2:
        return 0
    
    total = 0
    for i in range(len(points) - 1):
        dx = points[i+1][0] - points[i][0]
        dy = points[i+1][1] - points[i][1]
        total += math.sqrt(dx*dx + dy*dy)
    
    if is_closed and len(points) > 2:
        dx = points[0][0] - points[-1][0]
        dy = points[0][1] - points[-1][1]
        total += math.sqrt(dx*dx + dy*dy)
    
    return total


def is_likely_hole(diameter: float, max_hole_diameter: float = 2.0) -> bool:
    """Determine if a circle is likely a hole based on diameter"""
    return diameter <= max_hole_diameter


def is_bend_layer(layer_name: str) -> bool:
    """Check if layer name suggests bend lines"""
    bend_keywords = ['bend', 'fold', 'brake', 'crease', 'k-factor', 'kfactor']
    return any(kw in layer_name.lower() for kw in bend_keywords)


@router.post("/analyze", response_model=DXFAnalysisResult)
async def analyze_dxf(
    file: UploadFile = File(...),
    max_hole_diameter: float = 2.0,  # Circles larger than this are not holes
    bend_layer: Optional[str] = None,  # Specific layer name for bends
    units: str = "inches",  # inches or mm
    current_user: User = Depends(get_current_user)
):
    """
    Analyze a DXF file and extract sheet metal geometry data.
    
    Returns cut length, hole count, bend count, and flat pattern dimensions.
    """
    import ezdxf
    import tempfile
    import os
    
    # Validate file type
    if not file.filename.lower().endswith(('.dxf', '.DXF')):
        raise HTTPException(status_code=400, detail="File must be a DXF file")
    
    # Read file content
    content = await file.read()
    
    try:
        # Write to temp file - ezdxf works best with file paths
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.dxf', delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        
        try:
            doc = ezdxf.readfile(tmp_path)
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse DXF file: {str(e)}")
    
    # Get modelspace (where the geometry lives)
    msp = doc.modelspace()
    
    # Also check paperspace layouts and collect all entities
    all_entities = list(msp)
    
    # Check if modelspace is empty, try paperspace
    if len(all_entities) == 0:
        for layout in doc.layouts:
            if layout.name != 'Model':
                all_entities.extend(layout)
    
    # Also explode any block references to get their geometry
    for entity in list(all_entities):
        if entity.dxftype() == 'INSERT':
            try:
                block = doc.blocks.get(entity.dxf.name)
                if block:
                    all_entities.extend(block)
            except:
                pass
    
    # Initialize tracking variables
    min_x = float('inf')
    max_x = float('-inf')
    min_y = float('inf')
    max_y = float('-inf')
    
    total_cut_length = 0
    holes = []  # List of diameters
    slots = 0
    bends = []  # List of bend line lengths
    warnings = []
    layers = set()
    entity_count = 0
    
    # Unit conversion (if mm, convert to inches)
    scale = 1.0 if units == "inches" else 1/25.4
    
    # Process all entities
    for entity in all_entities:
        entity_count += 1
        layer = entity.dxf.layer
        layers.add(layer)
        
        try:
            if entity.dxftype() == 'LINE':
                # Line entity
                start = entity.dxf.start
                end = entity.dxf.end
                
                # Update bounding box
                min_x = min(min_x, start.x * scale, end.x * scale)
                max_x = max(max_x, start.x * scale, end.x * scale)
                min_y = min(min_y, start.y * scale, end.y * scale)
                max_y = max(max_y, start.y * scale, end.y * scale)
                
                # Calculate length
                length = math.sqrt((end.x - start.x)**2 + (end.y - start.y)**2) * scale
                
                # Check if this is a bend line
                if is_bend_layer(layer) or (bend_layer and layer.lower() == bend_layer.lower()):
                    bends.append(length)
                else:
                    total_cut_length += length
                    
            elif entity.dxftype() == 'CIRCLE':
                # Circle entity
                center = entity.dxf.center
                radius = entity.dxf.radius * scale
                diameter = radius * 2
                
                # Update bounding box
                min_x = min(min_x, (center.x - entity.dxf.radius) * scale)
                max_x = max(max_x, (center.x + entity.dxf.radius) * scale)
                min_y = min(min_y, (center.y - entity.dxf.radius) * scale)
                max_y = max(max_y, (center.y + entity.dxf.radius) * scale)
                
                # Circumference
                circumference = 2 * math.pi * radius
                
                # Is it a hole?
                if is_likely_hole(diameter, max_hole_diameter):
                    holes.append(diameter)
                    total_cut_length += circumference
                else:
                    # Large circle - probably outer profile
                    total_cut_length += circumference
                    
            elif entity.dxftype() == 'ARC':
                # Arc entity
                center = entity.dxf.center
                radius = entity.dxf.radius * scale
                start_angle = entity.dxf.start_angle
                end_angle = entity.dxf.end_angle
                
                # Update bounding box (approximate with center +/- radius)
                min_x = min(min_x, (center.x - entity.dxf.radius) * scale)
                max_x = max(max_x, (center.x + entity.dxf.radius) * scale)
                min_y = min(min_y, (center.y - entity.dxf.radius) * scale)
                max_y = max(max_y, (center.y + entity.dxf.radius) * scale)
                
                # Arc length
                arc_length = calculate_arc_length(start_angle, end_angle, radius)
                total_cut_length += arc_length
                
            elif entity.dxftype() in ('LWPOLYLINE', 'POLYLINE'):
                # Polyline entity
                try:
                    if entity.dxftype() == 'LWPOLYLINE':
                        points = [(p[0] * scale, p[1] * scale) for p in entity.get_points()]
                        is_closed = entity.closed
                    else:
                        points = [(v.dxf.location.x * scale, v.dxf.location.y * scale) 
                                  for v in entity.vertices]
                        is_closed = entity.is_closed
                    
                    # Update bounding box
                    for x, y in points:
                        min_x = min(min_x, x)
                        max_x = max(max_x, x)
                        min_y = min(min_y, y)
                        max_y = max(max_y, y)
                    
                    # Calculate length
                    length = calculate_polyline_length(points, is_closed)
                    
                    # Check if bend line
                    if is_bend_layer(layer) or (bend_layer and layer.lower() == bend_layer.lower()):
                        bends.append(length)
                    else:
                        total_cut_length += length
                        
                        # Check if it might be a slot (small closed elongated shape)
                        if is_closed and len(points) >= 4:
                            bbox_w = max(p[0] for p in points) - min(p[0] for p in points)
                            bbox_h = max(p[1] for p in points) - min(p[1] for p in points)
                            aspect = max(bbox_w, bbox_h) / max(min(bbox_w, bbox_h), 0.001)
                            area = bbox_w * bbox_h
                            if aspect > 2 and area < 4:  # Elongated and small
                                slots += 1
                                
                except Exception as e:
                    warnings.append(f"Could not process polyline: {str(e)}")
                    
            elif entity.dxftype() == 'ELLIPSE':
                # Ellipse - approximate circumference
                center = entity.dxf.center
                # Major axis is a vector from center
                major = entity.dxf.major_axis
                ratio = entity.dxf.ratio  # minor/major
                
                a = math.sqrt(major.x**2 + major.y**2) * scale  # semi-major
                b = a * ratio  # semi-minor
                
                # Approximate circumference (Ramanujan)
                h = ((a - b) / (a + b)) ** 2
                circumference = math.pi * (a + b) * (1 + (3 * h) / (10 + math.sqrt(4 - 3 * h)))
                
                total_cut_length += circumference
                
                # Update bounding box
                min_x = min(min_x, (center.x - a/scale) * scale)
                max_x = max(max_x, (center.x + a/scale) * scale)
                min_y = min(min_y, (center.y - b/scale) * scale)
                max_y = max(max_y, (center.y + b/scale) * scale)
                
            elif entity.dxftype() == 'SPLINE':
                # Spline - approximate with control points
                try:
                    points = [(p.x * scale, p.y * scale) for p in entity.control_points]
                    for x, y in points:
                        min_x = min(min_x, x)
                        max_x = max(max_x, x)
                        min_y = min(min_y, y)
                        max_y = max(max_y, y)
                    
                    # Approximate length (will be underestimate)
                    length = calculate_polyline_length(points, entity.closed)
                    total_cut_length += length * 1.1  # Add 10% for curve
                except:
                    warnings.append("Spline entity found - length approximated")
                    
        except Exception as e:
            warnings.append(f"Error processing {entity.dxftype()}: {str(e)}")
    
    # Handle case where no geometry found
    if min_x == float('inf'):
        raise HTTPException(status_code=400, detail="No geometry found in DXF file")
    
    # Calculate flat dimensions
    flat_length = max_x - min_x
    flat_width = max_y - min_y
    
    return DXFAnalysisResult(
        min_x=round(min_x, 4),
        max_x=round(max_x, 4),
        min_y=round(min_y, 4),
        max_y=round(max_y, 4),
        flat_length=round(flat_length, 3),
        flat_width=round(flat_width, 3),
        total_cut_length=round(total_cut_length, 2),
        num_holes=len(holes),
        num_slots=slots,
        num_bends=len(bends),
        num_entities=entity_count,
        hole_diameters=[round(d, 3) for d in sorted(holes)],
        bend_lengths=[round(b, 3) for b in bends],
        layers=sorted(list(layers)),
        warnings=warnings
    )


@router.post("/preview")
async def preview_dxf(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    """
    Get a simple text preview of DXF contents for debugging
    """
    import ezdxf
    import tempfile
    import os
    
    content = await file.read()
    
    try:
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.dxf', delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        
        try:
            doc = ezdxf.readfile(tmp_path)
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse: {str(e)}")
    
    msp = doc.modelspace()
    
    entity_types = {}
    layers = set()
    
    for entity in msp:
        etype = entity.dxftype()
        entity_types[etype] = entity_types.get(etype, 0) + 1
        layers.add(entity.dxf.layer)
    
    return {
        "filename": file.filename,
        "dxf_version": doc.dxfversion,
        "entity_counts": entity_types,
        "layers": sorted(list(layers)),
        "total_entities": sum(entity_types.values())
    }
