import {memo, useEffect, useRef, useState} from 'react';
import * as THREE from 'three';
import type {AnniePodPhase} from './annie-pod-state';

export interface AnnieParticleOrbProps {
  phase: AnniePodPhase;
  listeningWhilePlaying?: boolean;
}

interface StateConfig {
  name: string;
  colorTop: THREE.Color;
  colorMid: THREE.Color;
  colorBot: THREE.Color;
  nucleusColor: THREE.Color;
  ringColor: THREE.Color;
  speed: number;
  freq: number;
  morphAmp: number;
  breathAmp: number;
}

const ORB_STATES: Record<string, StateConfig> = {
  sleep: {
    name: 'SLEEP // 休眠待唤醒',
    colorTop: new THREE.Color(0xb2ccf2),
    colorMid: new THREE.Color(0x234d85),
    colorBot: new THREE.Color(0x0e1c3b),
    nucleusColor: new THREE.Color(0x050c1c),
    ringColor: new THREE.Color(0x356bb0),
    speed: 0.8,
    freq: 1.1,
    morphAmp: 0.08,
    breathAmp: 0.06,
  },
  awake: {
    name: 'AWAKE // 在线待命',
    colorTop: new THREE.Color(0xffffff),
    colorMid: new THREE.Color(0x00f0ff),
    colorBot: new THREE.Color(0x0066ff),
    nucleusColor: new THREE.Color(0x041c38),
    ringColor: new THREE.Color(0x00f0ff),
    speed: 1.4,
    freq: 1.8,
    morphAmp: 0.14,
    breathAmp: 0.10,
  },
  followup: {
    name: 'FOLLOWUP // 等待追问',
    colorTop: new THREE.Color(0xffffff),
    colorMid: new THREE.Color(0x00ffc4),
    colorBot: new THREE.Color(0x0077aa),
    nucleusColor: new THREE.Color(0x042028),
    ringColor: new THREE.Color(0x00ffc4),
    speed: 1.6,
    freq: 2.1,
    morphAmp: 0.16,
    breathAmp: 0.12,
  },
  listening: {
    name: 'LISTENING // 正在收听',
    colorTop: new THREE.Color(0xffffff),
    colorMid: new THREE.Color(0x00f5ff),
    colorBot: new THREE.Color(0x0044ff),
    nucleusColor: new THREE.Color(0x041b36),
    ringColor: new THREE.Color(0x00f5ff),
    speed: 1.6,
    freq: 2.4,
    morphAmp: 0.18,
    breathAmp: 0.14,
  },
  processing: {
    name: 'PROCESSING // 深度联想',
    colorTop: new THREE.Color(0x00ffff),
    colorMid: new THREE.Color(0x9d4edd),
    colorBot: new THREE.Color(0x3c096c),
    nucleusColor: new THREE.Color(0x1a0636),
    ringColor: new THREE.Color(0xc77dff),
    speed: 2.9,
    freq: 4.4,
    morphAmp: 0.28,
    breathAmp: 0.16,
  },
  playing: {
    name: 'PLAYING // 声光电合成',
    colorTop: new THREE.Color(0xffffff),
    colorMid: new THREE.Color(0x00f5ff),
    colorBot: new THREE.Color(0xff2a85),
    nucleusColor: new THREE.Color(0x0a1638),
    ringColor: new THREE.Color(0xec4899),
    speed: 2.3,
    freq: 3.2,
    morphAmp: 0.24,
    breathAmp: 0.20,
  },
  barge_in: {
    name: 'BARGE_IN // 突发打断激波',
    colorTop: new THREE.Color(0xffffff),
    colorMid: new THREE.Color(0xff1144),
    colorBot: new THREE.Color(0x770022),
    nucleusColor: new THREE.Color(0x380510),
    ringColor: new THREE.Color(0xff0055),
    speed: 4.5,
    freq: 6.2,
    morphAmp: 0.46,
    breathAmp: 0.36,
  },
  error: {
    name: 'ERROR // 交互异常',
    colorTop: new THREE.Color(0xffccd5),
    colorMid: new THREE.Color(0xff2a55),
    colorBot: new THREE.Color(0x660018),
    nucleusColor: new THREE.Color(0x28000a),
    ringColor: new THREE.Color(0xff2a55),
    speed: 1.8,
    freq: 3.2,
    morphAmp: 0.24,
    breathAmp: 0.15,
  },
  offline: {
    name: 'OFFLINE // 未连接',
    colorTop: new THREE.Color(0x8fa2b8),
    colorMid: new THREE.Color(0x3c4f69),
    colorBot: new THREE.Color(0x182233),
    nucleusColor: new THREE.Color(0x0c121c),
    ringColor: new THREE.Color(0x4b6182),
    speed: 0.5,
    freq: 0.8,
    morphAmp: 0.05,
    breathAmp: 0.04,
  },
};

function noise3D(x: number, y: number, z: number): number {
  return Math.sin(x * 1.5 + y * 2.1) * Math.cos(y * 1.8 + z * 1.4) + Math.sin(z * 2.3 + x * 0.9) * 0.5;
}

function AnnieParticleOrbImpl({phase, listeningWhilePlaying}: AnnieParticleOrbProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [unsupported, setUnsupported] = useState(false);

  const activeKey = listeningWhilePlaying ? 'barge_in' : (phase in ORB_STATES ? phase : 'offline');
  const targetRef = useRef<StateConfig>(ORB_STATES[activeKey] ?? ORB_STATES.offline);

  useEffect(() => {
    targetRef.current = ORB_STATES[activeKey] ?? ORB_STATES.offline;
  }, [activeKey]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({
        alpha: true,
        antialias: true,
        powerPreference: 'high-performance',
      });
    } catch {
      setUnsupported(true);
      return;
    }

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);
    camera.position.set(0, 0, 14.5);

    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.domElement.className = 'annie-particle-orb-canvas';
    container.appendChild(renderer.domElement);

    const orbGroup = new THREE.Group();
    scene.add(orbGroup);

    // 1. 4,200 micro particles forming dynamic bioluminescent sphere
    const particleCount = 4200;
    const pGeom = new THREE.BufferGeometry();
    const basePositions = new Float32Array(particleCount * 3);
    const currentPositions = new Float32Array(particleCount * 3);
    const particleColors = new Float32Array(particleCount * 3);
    const particleNormY = new Float32Array(particleCount);
    const radius = 3.6;

    for (let i = 0; i < particleCount; i++) {
      const phi = Math.acos(1 - 2 * (i + 0.5) / particleCount);
      const theta = Math.PI * (1 + Math.sqrt(5)) * (i + 0.5);

      const x = radius * Math.sin(phi) * Math.cos(theta);
      const y = radius * Math.sin(phi) * Math.sin(theta);
      const z = radius * Math.cos(phi);

      basePositions[i * 3] = x;
      basePositions[i * 3 + 1] = y;
      basePositions[i * 3 + 2] = z;

      currentPositions[i * 3] = x;
      currentPositions[i * 3 + 1] = y;
      currentPositions[i * 3 + 2] = z;

      particleNormY[i] = (y / radius + 1) * 0.5;

      particleColors[i * 3] = 0.0;
      particleColors[i * 3 + 1] = 0.96;
      particleColors[i * 3 + 2] = 1.0;
    }

    pGeom.setAttribute('position', new THREE.BufferAttribute(currentPositions, 3));
    pGeom.setAttribute('color', new THREE.BufferAttribute(particleColors, 3));

    const pMat = new THREE.PointsMaterial({
      size: 0.13,
      vertexColors: true,
      transparent: true,
      opacity: 0.92,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const fluidParticleMesh = new THREE.Points(pGeom, pMat);
    orbGroup.add(fluidParticleMesh);

    // 2. Inner Glow Nucleus
    const nucleusGeom = new THREE.SphereGeometry(2.5, 48, 48);
    const nucleusMat = new THREE.MeshBasicMaterial({
      color: 0x061838,
      transparent: true,
      opacity: 0.65,
    });
    const nucleusMesh = new THREE.Mesh(nucleusGeom, nucleusMat);
    orbGroup.add(nucleusMesh);

    // 3. Acoustic Ripple Wave Rings
    const ringGroup = new THREE.Group();
    const rippleCount = 3;
    const rippleRings: THREE.Mesh<THREE.RingGeometry, THREE.MeshBasicMaterial>[] = [];

    for (let r = 0; r < rippleCount; r++) {
      const rGeom = new THREE.RingGeometry(3.7 + r * 0.45, 3.76 + r * 0.45, 96);
      const rMat = new THREE.MeshBasicMaterial({
        color: 0x00f5ff,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: 0.35 - r * 0.08,
        blending: THREE.AdditiveBlending,
      });
      const ring = new THREE.Mesh(rGeom, rMat);
      ring.rotation.x = Math.PI / 2 + (r - 1) * 0.22;
      ring.rotation.y = (r - 1) * 0.18;
      ringGroup.add(ring);
      rippleRings.push(ring);
    }
    orbGroup.add(ringGroup);

    // 4. Ambient Micro Dust Starlight Field
    const starlightCount = 280;
    const starGeom = new THREE.BufferGeometry();
    const starPos = new Float32Array(starlightCount * 3);
    const starCol = new Float32Array(starlightCount * 3);

    for (let s = 0; s < starlightCount; s++) {
      const dist = 4.2 + Math.random() * 5.2;
      const th = Math.random() * Math.PI * 2;
      const ph = Math.acos(2 * Math.random() - 1);
      starPos[s * 3] = dist * Math.sin(ph) * Math.cos(th);
      starPos[s * 3 + 1] = dist * Math.sin(ph) * Math.sin(th);
      starPos[s * 3 + 2] = dist * Math.cos(ph);

      starCol[s * 3] = 0.2;
      starCol[s * 3 + 1] = 0.8;
      starCol[s * 3 + 2] = 1.0;
    }
    starGeom.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
    starGeom.setAttribute('color', new THREE.BufferAttribute(starCol, 3));
    const starMat = new THREE.PointsMaterial({
      size: 0.05,
      vertexColors: true,
      transparent: true,
      opacity: 0.5,
      blending: THREE.AdditiveBlending,
    });
    const starlightMesh = new THREE.Points(starGeom, starMat);
    orbGroup.add(starlightMesh);

    // Initial state matching current active configuration
    const initialConfig = targetRef.current;
    const currState = {
      colorTop: initialConfig.colorTop.clone(),
      colorMid: initialConfig.colorMid.clone(),
      colorBot: initialConfig.colorBot.clone(),
      nucleusColor: initialConfig.nucleusColor.clone(),
      ringColor: initialConfig.ringColor.clone(),
      speed: initialConfig.speed,
      freq: initialConfig.freq,
      morphAmp: initialConfig.morphAmp,
      breathAmp: initialConfig.breathAmp,
    };

    const tempCol = new THREE.Color();
    const clock = new THREE.Clock();
    let rafId = 0;
    let disposed = false;
    let morphPhase = 0;
    let voicePhase = 0;
    let nucleusPhase = 0;
    let ringPhase = 0;
    let rotY = 0;
    let rotX = 0;
    let rotZ = 0;
    let starRot = 0;

    const handleResize = () => {
      if (disposed || !container) return;
      const w = container.clientWidth || 260;
      const h = container.clientHeight || 240;
      const aspect = w / Math.max(h, 1);
      camera.aspect = aspect;
      // Auto-fit distance so sphere is never cropped when container is narrow
      const fitZ = aspect < 1 ? 14.5 * (1 / aspect) * 0.92 : 14.5;
      camera.position.set(0, 0, Math.min(Math.max(fitZ, 12), 22));
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
    };

    handleResize();

    const resizeObserver = typeof ResizeObserver !== 'undefined'
      ? new ResizeObserver(() => handleResize())
      : null;
    if (resizeObserver) resizeObserver.observe(container);

    const animate = () => {
      if (disposed) return;
      rafId = requestAnimationFrame(animate);

      if (document.hidden) return;

      const delta = Math.min(clock.getDelta(), 0.05);
      const target = targetRef.current;

      // 1. Smooth chromatic transition lerp (1.2s continuous glide)
      const colorLerp = Math.min(1.0, delta * 1.8);
      currState.colorTop.lerp(target.colorTop, colorLerp);
      currState.colorMid.lerp(target.colorMid, colorLerp);
      currState.colorBot.lerp(target.colorBot, colorLerp);
      currState.nucleusColor.lerp(target.nucleusColor, colorLerp);
      currState.ringColor.lerp(target.ringColor, colorLerp);

      // 2. Smooth physics parameter lerp
      const physicsLerp = Math.min(1.0, delta * 2.0);
      currState.speed += (target.speed - currState.speed) * physicsLerp;
      currState.freq += (target.freq - currState.freq) * physicsLerp;
      currState.morphAmp += (target.morphAmp - currState.morphAmp) * physicsLerp;
      currState.breathAmp += (target.breathAmp - currState.breathAmp) * physicsLerp;

      // 3. Continuous phase integration (PREVENTS sudden acceleration spikes on state switch)
      morphPhase += delta * currState.speed * 0.75;
      voicePhase += delta * currState.freq;
      nucleusPhase += delta * currState.freq * 0.8;
      ringPhase += delta * currState.freq * 0.9;
      rotY += delta * 0.22;
      rotX += delta * 0.16;
      rotZ += delta * 0.12;
      starRot -= delta * 0.05;

      nucleusMat.color.copy(currState.nucleusColor);
      rippleRings.forEach(r => r.material.color.copy(currState.ringColor));

      const posArr = fluidParticleMesh.geometry.attributes.position.array as Float32Array;
      const colArr = fluidParticleMesh.geometry.attributes.color.array as Float32Array;

      const voiceEnergy = 0.5 + Math.sin(voicePhase) * 0.25 + Math.cos(voicePhase * 1.7) * 0.12;

      for (let i = 0; i < particleCount; i++) {
        const bx = basePositions[i * 3];
        const by = basePositions[i * 3 + 1];
        const bz = basePositions[i * 3 + 2];

        const n = noise3D(bx * 0.38 + morphPhase * 0.32, by * 0.38 + morphPhase * 0.26, bz * 0.38 + morphPhase * 0.22);
        const n2 = Math.sin(bx * 0.75 + morphPhase * 0.65) * Math.cos(by * 0.75 + morphPhase * 0.48);

        const displacement = 1.0 + (n * currState.morphAmp + n2 * (currState.morphAmp * 0.5)) * (0.8 + voiceEnergy * 0.6);

        posArr[i * 3] = bx * displacement;
        posArr[i * 3 + 1] = by * displacement;
        posArr[i * 3 + 2] = bz * displacement;

        const normY = particleNormY[i];
        if (normY > 0.65) {
          tempCol.lerpColors(currState.colorMid, currState.colorTop, (normY - 0.65) / 0.35);
        } else {
          tempCol.lerpColors(currState.colorBot, currState.colorMid, normY / 0.65);
        }

        colArr[i * 3] = tempCol.r;
        colArr[i * 3 + 1] = tempCol.g;
        colArr[i * 3 + 2] = tempCol.b;
      }
      fluidParticleMesh.geometry.attributes.position.needsUpdate = true;
      fluidParticleMesh.geometry.attributes.color.needsUpdate = true;

      orbGroup.rotation.y = rotY;
      orbGroup.rotation.x = Math.sin(rotX) * 0.12;
      orbGroup.rotation.z = Math.cos(rotZ) * 0.08;

      const nucleusScale = 0.92 + Math.sin(nucleusPhase) * currState.breathAmp;
      nucleusMesh.scale.set(nucleusScale, nucleusScale, nucleusScale);

      rippleRings.forEach((r, idx) => {
        r.rotation.z = (ringPhase * 0.3 + idx * 0.6) * (idx % 2 === 0 ? 1 : -1);
        const s = 1.0 + Math.sin(ringPhase + idx * 1.4) * (currState.breathAmp * 0.6);
        r.scale.set(s, s, s);
      });

      starlightMesh.rotation.y = starRot;

      renderer.render(scene, camera);
    };

    animate();

    return () => {
      disposed = true;
      cancelAnimationFrame(rafId);
      resizeObserver?.disconnect();
      pGeom.dispose();
      pMat.dispose();
      nucleusGeom.dispose();
      nucleusMat.dispose();
      rippleRings.forEach(r => {
        r.geometry.dispose();
        r.material.dispose();
      });
      starGeom.dispose();
      starMat.dispose();
      renderer.dispose();
      if (container && renderer.domElement.parentElement === container) {
        container.removeChild(renderer.domElement);
      }
    };
  }, []);

  return <div className="annie-particle-orb-container" ref={containerRef} aria-hidden="true">
    {unsupported && (
      <div className="annie-particle-orb-fallback">
        <div className="annie-pod-scan"/>
        <div className="annie-pod-ring inner"/>
        <div className="annie-pod-aura"><span>A</span></div>
      </div>
    )}
  </div>;
}

export const AnnieParticleOrb = memo(AnnieParticleOrbImpl);
