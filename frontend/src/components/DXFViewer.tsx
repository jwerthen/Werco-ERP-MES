import React, { useEffect, useRef, useState } from 'react';
import { ArrowsPointingOutIcon, ArrowsPointingInIcon } from '@heroicons/react/24/outline';

interface DXFViewerProps {
  file: File;
  analysis?: {
    min_x: number;
    max_x: number;
    min_y: number;
    max_y: number;
    flat_length: number;
    flat_width: number;
  };
}

export default function DXFViewer({ file, analysis }: DXFViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [dxfContent, setDxfContent] = useState<string>('');
  const [expanded, setExpanded] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const reader = new FileReader();
    reader.onload = (e) => {
      setDxfContent(e.target?.result as string);
    };
    reader.onerror = () => setError('Failed to read file');
    reader.readAsText(file);
  }, [file]);

  useEffect(() => {
    if (!dxfContent || !canvasRef.current) return;

    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // Handle high-DPI displays
    const dpr = window.devicePixelRatio || 1;
    const displayWidth = expanded ? window.innerWidth - 64 : 400;
    const displayHeight = expanded ? window.innerHeight - 64 : 250;
    
    // Set actual canvas size in memory (scaled for DPI)
    canvas.width = displayWidth * dpr;
    canvas.height = displayHeight * dpr;
    
    // Scale context to match DPI
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // Parse and render DXF
    try {
      renderDXF(ctx, displayWidth, displayHeight, dxfContent, analysis);
    } catch (err) {
      console.error('DXF render error:', err);
      setError('Could not render DXF preview');
    }
  }, [dxfContent, analysis, expanded]);

  if (error) {
    return (
      <div className="bg-gray-100 rounded p-4 text-center text-gray-500 text-sm">
        {error}
      </div>
    );
  }

  const displayWidth = expanded ? window.innerWidth - 64 : 400;
  const displayHeight = expanded ? window.innerHeight - 64 : 250;

  return (
    <div className={`relative bg-gray-900 rounded-lg overflow-hidden ${expanded ? 'fixed inset-4 z-50' : ''}`}>
      <div className="absolute top-2 right-2 z-10 flex gap-2">
        <button
          onClick={() => setExpanded(!expanded)}
          className="bg-white/90 hover:bg-white p-1.5 rounded shadow"
          title={expanded ? 'Collapse' : 'Expand'}
        >
          {expanded ? (
            <ArrowsPointingInIcon className="h-5 w-5 text-gray-700" />
          ) : (
            <ArrowsPointingOutIcon className="h-5 w-5 text-gray-700" />
          )}
        </button>
      </div>
      {expanded && (
        <div 
          className="fixed inset-0 bg-black/50 -z-10" 
          onClick={() => setExpanded(false)}
        />
      )}
      <canvas
        ref={canvasRef}
        style={{ width: displayWidth, height: displayHeight }}
        className="block"
      />
      {analysis && (
        <div className="absolute bottom-2 left-2 bg-black/70 text-white text-xs px-2 py-1 rounded">
          {analysis.flat_length.toFixed(2)}" × {analysis.flat_width.toFixed(2)}"
        </div>
      )}
    </div>
  );
}

interface Point {
  x: number;
  y: number;
}

function renderDXF(
  ctx: CanvasRenderingContext2D, 
  canvasWidth: number,
  canvasHeight: number, 
  content: string,
  analysis?: { min_x: number; max_x: number; min_y: number; max_y: number }
) {
  // Clear canvas
  ctx.fillStyle = '#1a1a2e';
  ctx.fillRect(0, 0, canvasWidth, canvasHeight);

  // Parse DXF entities
  const entities = parseDXFEntities(content);
  
  if (entities.length === 0) {
    ctx.fillStyle = '#666';
    ctx.font = '14px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('No geometry found', canvasWidth / 2, canvasHeight / 2);
    return;
  }

  // Calculate bounds
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  
  if (analysis) {
    minX = analysis.min_x;
    maxX = analysis.max_x;
    minY = analysis.min_y;
    maxY = analysis.max_y;
  } else {
    entities.forEach(e => {
      if (e.type === 'LINE') {
        minX = Math.min(minX, e.start.x, e.end.x);
        maxX = Math.max(maxX, e.start.x, e.end.x);
        minY = Math.min(minY, e.start.y, e.end.y);
        maxY = Math.max(maxY, e.start.y, e.end.y);
      } else if (e.type === 'CIRCLE' || e.type === 'ARC') {
        minX = Math.min(minX, e.center.x - e.radius);
        maxX = Math.max(maxX, e.center.x + e.radius);
        minY = Math.min(minY, e.center.y - e.radius);
        maxY = Math.max(maxY, e.center.y + e.radius);
      } else if (e.type === 'LWPOLYLINE' && e.points.length > 0) {
        e.points.forEach((p: Point) => {
          minX = Math.min(minX, p.x);
          maxX = Math.max(maxX, p.x);
          minY = Math.min(minY, p.y);
          maxY = Math.max(maxY, p.y);
        });
      }
    });
  }

  // Add padding
  const padding = 20;
  const width = maxX - minX || 1;
  const height = maxY - minY || 1;
  
  // Calculate scale to fit
  const scaleX = (canvasWidth - padding * 2) / width;
  const scaleY = (canvasHeight - padding * 2) / height;
  const scale = Math.min(scaleX, scaleY);

  // Transform function
  const tx = (x: number) => padding + (x - minX) * scale;
  const ty = (y: number) => canvasHeight - padding - (y - minY) * scale; // Flip Y

  // Draw grid
  ctx.strokeStyle = '#2a2a4a';
  ctx.lineWidth = 0.5;
  const gridSize = Math.pow(10, Math.floor(Math.log10(width / 5)));
  for (let x = Math.floor(minX / gridSize) * gridSize; x <= maxX; x += gridSize) {
    ctx.beginPath();
    ctx.moveTo(tx(x), 0);
    ctx.lineTo(tx(x), canvasHeight);
    ctx.stroke();
  }
  for (let y = Math.floor(minY / gridSize) * gridSize; y <= maxY; y += gridSize) {
    ctx.beginPath();
    ctx.moveTo(0, ty(y));
    ctx.lineTo(canvasWidth, ty(y));
    ctx.stroke();
  }

  // Draw entities
  ctx.strokeStyle = '#00ff88';
  ctx.lineWidth = 1.5;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';

  entities.forEach(e => {
    ctx.beginPath();
    
    // Color by layer (bend lines different color)
    if (e.layer && /bend|fold|brake/i.test(e.layer)) {
      ctx.strokeStyle = '#ff6b6b';
    } else {
      ctx.strokeStyle = '#00ff88';
    }

    if (e.type === 'LINE') {
      ctx.moveTo(tx(e.start.x), ty(e.start.y));
      ctx.lineTo(tx(e.end.x), ty(e.end.y));
      ctx.stroke();
    } else if (e.type === 'CIRCLE' && e.radius > 0) {
      ctx.beginPath();
      ctx.arc(tx(e.center.x), ty(e.center.y), e.radius * scale, 0, Math.PI * 2);
      ctx.stroke();
    } else if (e.type === 'ARC' && e.radius > 0) {
      ctx.beginPath();
      // DXF angles are in degrees, canvas uses radians
      // Also need to flip because Y is inverted
      const startRad = -(e.endAngle || 360) * Math.PI / 180;
      const endRad = -(e.startAngle || 0) * Math.PI / 180;
      ctx.arc(tx(e.center.x), ty(e.center.y), e.radius * scale, startRad, endRad);
      ctx.stroke();
    } else if (e.type === 'LWPOLYLINE' && e.points && e.points.length > 0) {
      ctx.moveTo(tx(e.points[0].x), ty(e.points[0].y));
      for (let i = 1; i < e.points.length; i++) {
        ctx.lineTo(tx(e.points[i].x), ty(e.points[i].y));
      }
      if (e.closed) {
        ctx.closePath();
      }
      ctx.stroke();
    }
  });

  // Draw origin marker
  if (minX <= 0 && maxX >= 0 && minY <= 0 && maxY >= 0) {
    ctx.strokeStyle = '#666';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(tx(0) - 10, ty(0));
    ctx.lineTo(tx(0) + 10, ty(0));
    ctx.moveTo(tx(0), ty(0) - 10);
    ctx.lineTo(tx(0), ty(0) + 10);
    ctx.stroke();
  }
}

interface DXFEntity {
  type: string;
  layer?: string;
  start: Point;
  end: Point;
  center: Point;
  radius: number;
  startAngle: number;
  endAngle: number;
  points: Point[];
  closed: boolean;
}

function parseDXFEntities(content: string): DXFEntity[] {
  const entities: DXFEntity[] = [];
  const lines = content.split(/\r?\n/);
  
  let inEntities = false;
  let currentEntity: DXFEntity | null = null;
  let currentCode = '';
  let polylinePoints: Point[] = [];
  let currentPoint: Partial<Point> = {};

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    
    if (line === 'ENTITIES') {
      inEntities = true;
      continue;
    }
    if (line === 'ENDSEC' && inEntities) {
      if (currentEntity) {
        if (currentEntity.type === 'LWPOLYLINE' && polylinePoints.length > 0) {
          currentEntity.points = [...polylinePoints];
        }
        entities.push(currentEntity);
      }
      break;
    }
    
    if (!inEntities) continue;

    // DXF files alternate between group codes and values
    if (i % 2 === 0 || lines[i - 1]?.trim().match(/^\d+$/)) {
      // This might be a group code
      if (/^\s*\d+\s*$/.test(line)) {
        currentCode = line.trim();
        continue;
      }
    }

    // Process based on current code
    const code = parseInt(currentCode);
    const value = line;

    if (code === 0) {
      // New entity or end of current
      if (currentEntity) {
        if (currentEntity.type === 'LWPOLYLINE' && polylinePoints.length > 0) {
          currentEntity.points = [...polylinePoints];
        }
        entities.push(currentEntity);
      }
      
      polylinePoints = [];
      currentPoint = {};
      
      const defaultEntity: DXFEntity = {
          type: value,
          start: { x: 0, y: 0 },
          end: { x: 0, y: 0 },
          center: { x: 0, y: 0 },
          radius: 0,
          startAngle: 0,
          endAngle: 360,
          points: [],
          closed: false
        };
      
      if (value === 'LINE' || value === 'CIRCLE' || value === 'ARC' || value === 'LWPOLYLINE') {
        currentEntity = { ...defaultEntity, type: value };
      } else {
        currentEntity = null;
      }
    } else if (currentEntity) {
      const numValue = parseFloat(value);
      
      // Layer
      if (code === 8) {
        currentEntity.layer = value;
      }
      
      // LINE coordinates
      if (currentEntity.type === 'LINE') {
        if (code === 10) currentEntity.start!.x = numValue;
        if (code === 20) currentEntity.start!.y = numValue;
        if (code === 11) currentEntity.end!.x = numValue;
        if (code === 21) currentEntity.end!.y = numValue;
      }
      
      // CIRCLE/ARC coordinates
      if (currentEntity.type === 'CIRCLE' || currentEntity.type === 'ARC') {
        if (code === 10) currentEntity.center!.x = numValue;
        if (code === 20) currentEntity.center!.y = numValue;
        if (code === 40) currentEntity.radius = numValue;
        if (code === 50) currentEntity.startAngle = numValue;
        if (code === 51) currentEntity.endAngle = numValue;
      }
      
      // LWPOLYLINE
      if (currentEntity.type === 'LWPOLYLINE') {
        if (code === 70) currentEntity.closed = (parseInt(value) & 1) === 1;
        if (code === 10) {
          if (currentPoint.x !== undefined && currentPoint.y !== undefined) {
            polylinePoints.push({ x: currentPoint.x, y: currentPoint.y });
          }
          currentPoint = { x: numValue };
        }
        if (code === 20) {
          currentPoint.y = numValue;
          if (currentPoint.x !== undefined) {
            polylinePoints.push({ x: currentPoint.x, y: currentPoint.y });
            currentPoint = {};
          }
        }
      }
    }
    
    currentCode = '';
  }

  return entities;
}
