import {useEffect,useRef} from 'react';

export type ColorPalette = 'violet' | 'emerald' | 'amber' | 'blue';

interface ParticleCanvasProps {
  theme?: 'dark' | 'light';
  palette?: ColorPalette;
  micActive?: boolean;
  speechActive?: boolean;
  className?: string;
}

interface Particle {
  x: number;
  y: number;
  vx: number;
  vy: number;
  radius: number;
  alpha: number;
  targetAlpha: number;
  colorType: number;
  phase: number;
}

export function ParticleCanvas({
  theme = 'dark',
  palette = 'violet',
  micActive = false,
  speechActive = false,
  className = '',
}: ParticleCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animId: number | null = null;
    let width = 0;
    let height = 0;
    let dpr = Math.min(window.devicePixelRatio || 1, 2);

    const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    const resize = () => {
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    // Color definitions based on theme and selected palette
    const getColors = () => {
      if (theme === 'light') {
        if (palette === 'emerald') {
          return [
            'rgba(16, 185, 129,',
            'rgba(5, 150, 105,',
            'rgba(13, 148, 136,',
            'rgba(100, 116, 139,',
          ];
        }
        if (palette === 'amber') {
          return [
            'rgba(245, 158, 11,',
            'rgba(234, 88, 12,',
            'rgba(225, 29, 72,',
            'rgba(100, 116, 139,',
          ];
        }
        if (palette === 'blue') {
          return [
            'rgba(37, 99, 235,',
            'rgba(2, 132, 199,',
            'rgba(59, 130, 246,',
            'rgba(100, 116, 139,',
          ];
        }
        return [
          'rgba(139, 92, 246,',
          'rgba(99, 102, 241,',
          'rgba(217, 70, 239,',
          'rgba(100, 116, 139,',
        ];
      }

      // Dark Theme Palettes
      if (palette === 'emerald') {
        return [
          'rgba(16, 185, 129,',  // Emerald
          'rgba(52, 211, 153,',  // Mint
          'rgba(6, 182, 212,',   // Cyan
          'rgba(132, 204, 22,',  // Lime
        ];
      }
      if (palette === 'amber') {
        return [
          'rgba(245, 158, 11,',  // Amber
          'rgba(249, 115, 22,',  // Orange
          'rgba(244, 63, 94,',   // Rose
          'rgba(251, 191, 36,',  // Gold
        ];
      }
      if (palette === 'blue') {
        return [
          'rgba(59, 130, 246,',  // Blue
          'rgba(56, 189, 248,',  // Cyan
          'rgba(14, 165, 233,',  // Sky
          'rgba(99, 102, 241,',  // Indigo
        ];
      }
      // Default: Nebula Violet
      return [
        'rgba(139, 92, 246,',  // Violet
        'rgba(168, 85, 247,',  // Purple
        'rgba(244, 63, 94,',   // Rose pink
        'rgba(56, 189, 248,',  // Cyan star
      ];
    };

    const count = Math.min(50, Math.max(25, Math.floor((width * height) / 18000)));
    const particles: Particle[] = [];

    for (let i = 0; i < count; i++) {
      particles.push({
        x: Math.random() * (width || 800),
        y: Math.random() * (height || 600),
        vx: (Math.random() - 0.5) * 0.45,
        vy: (Math.random() - 0.5) * 0.45,
        radius: Math.random() * 1.8 + 1.2,
        alpha: Math.random() * 0.4 + 0.15,
        targetAlpha: Math.random() * 0.5 + 0.2,
        colorType: Math.floor(Math.random() * 4),
        phase: Math.random() * Math.PI * 2,
      });
    }

    let time = 0;

    const render = () => {
      if (!ctx || width === 0 || height === 0) return;
      ctx.clearRect(0, 0, width, height);

      const colors = getColors();
      const isDark = theme === 'dark';
      time += 0.015;

      const energy = speechActive ? 2.2 : micActive ? 1.6 : 1.0;

      // Draw subtle ambient glow orbs in the background
      const centerX = width * 0.5;
      const centerY = height * 0.38;
      const pulse = Math.sin(time * (speechActive ? 3.5 : 1.2)) * 0.05 + 0.95;

      const grad = ctx.createRadialGradient(
        centerX,
        centerY,
        10,
        centerX,
        centerY,
        Math.max(width, height) * 0.45 * pulse
      );

      const primaryRgb = colors[0];
      const secondaryRgb = colors[1];

      if (isDark) {
        if (speechActive) {
          grad.addColorStop(0, `${primaryRgb}0.16)`);
          grad.addColorStop(0.5, `${secondaryRgb}0.07)`);
          grad.addColorStop(1, 'rgba(9, 13, 22, 0)');
        } else if (micActive) {
          grad.addColorStop(0, `${secondaryRgb}0.18)`);
          grad.addColorStop(0.5, `${primaryRgb}0.08)`);
          grad.addColorStop(1, 'rgba(9, 13, 22, 0)');
        } else {
          grad.addColorStop(0, `${primaryRgb}0.09)`);
          grad.addColorStop(0.6, `${secondaryRgb}0.04)`);
          grad.addColorStop(1, 'rgba(9, 13, 22, 0)');
        }
      } else {
        if (speechActive) {
          grad.addColorStop(0, `${primaryRgb}0.1)`);
          grad.addColorStop(0.6, `${secondaryRgb}0.04)`);
          grad.addColorStop(1, 'rgba(248, 250, 252, 0)');
        } else if (micActive) {
          grad.addColorStop(0, `${secondaryRgb}0.12)`);
          grad.addColorStop(0.6, `${primaryRgb}0.04)`);
          grad.addColorStop(1, 'rgba(248, 250, 252, 0)');
        } else {
          grad.addColorStop(0, `${primaryRgb}0.06)`);
          grad.addColorStop(0.7, `${secondaryRgb}0.02)`);
          grad.addColorStop(1, 'rgba(248, 250, 252, 0)');
        }
      }

      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, width, height);

      // Draw and update particles
      const maxDist = isDark ? 95 : 85;

      for (let i = 0; i < particles.length; i++) {
        const p = particles[i];

        if (!prefersReducedMotion) {
          p.x += p.vx * energy;
          p.y += p.vy * energy;

          if (speechActive) {
            p.y += Math.sin(p.x * 0.015 + time * 4) * 0.45;
          }

          if (p.x < -10) p.x = width + 10;
          if (p.x > width + 10) p.x = -10;
          if (p.y < -10) p.y = height + 10;
          if (p.y > height + 10) p.y = -10;
        }

        const pAlpha = p.alpha * (Math.sin(time + p.phase) * 0.3 + 0.85) * (speechActive ? 1.4 : micActive ? 1.2 : 1.0);
        const col = colors[p.colorType];

        ctx.beginPath();
        ctx.arc(p.x, p.y, p.radius * (speechActive ? 1.3 : 1.0), 0, Math.PI * 2);
        ctx.fillStyle = `${col}${Math.min(1, Math.max(0, pAlpha))})`;
        ctx.shadowBlur = isDark ? 8 : 4;
        ctx.shadowColor = `${col}0.6)`;
        ctx.fill();
        ctx.shadowBlur = 0;

        for (let j = i + 1; j < particles.length; j++) {
          const p2 = particles[j];
          const dx = p.x - p2.x;
          const dy = p.y - p2.y;
          const dist = Math.sqrt(dx * dx + dy * dy);

          if (dist < maxDist) {
            const lineAlpha = (1 - dist / maxDist) * (isDark ? 0.22 : 0.14) * (speechActive ? 1.5 : 1.0);
            ctx.beginPath();
            ctx.moveTo(p.x, p.y);
            ctx.lineTo(p2.x, p2.y);
            ctx.strokeStyle = `${col}${Math.min(1, lineAlpha)})`;
            ctx.lineWidth = 0.85;
            ctx.stroke();
          }
        }
      }

      if (!prefersReducedMotion) {
        animId = requestAnimationFrame(render);
      }
    };

    if (prefersReducedMotion) {
      render();
    } else {
      animId = requestAnimationFrame(render);
    }

    const handleVisibility = () => {
      if (document.hidden) {
        if (animId) cancelAnimationFrame(animId);
      } else if (!prefersReducedMotion) {
        animId = requestAnimationFrame(render);
      }
    };

    document.addEventListener('visibilitychange', handleVisibility);

    return () => {
      if (animId) cancelAnimationFrame(animId);
      ro.disconnect();
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, [theme, palette, micActive, speechActive]);

  return (
    <canvas
      ref={canvasRef}
      className={`particle-canvas ${className}`}
      aria-hidden="true"
    />
  );
}
