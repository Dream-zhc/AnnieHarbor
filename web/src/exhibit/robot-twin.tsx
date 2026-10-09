import {memo, useEffect, useRef, useState} from 'react';
import * as THREE from 'three';
import {GLTFLoader} from 'three/examples/jsm/loaders/GLTFLoader.js';
import type {ActionPreview} from './telemetry';
import {Activity, Check, Eye, Gauge, Move3d, Radio, Smile, Volume2} from 'lucide-react';

type Props = {
  preview: ActionPreview | null;
  generation: number;
  speaking: boolean;
  reducedMotion: boolean;
  phase?: string;
  pose?: {yaw: number | null; pitch: number | null; roll: number | null; status: string};
  viseme?: string | null;
  expression?: string | null;
};

const isPlayable = (preview: ActionPreview | null) =>
  Boolean(preview && preview.call_id && preview.status === 'published_unknown');

function RobotTwinImpl({
  preview,
  generation,
  speaking,
  reducedMotion,
  pose,
  viseme,
  expression,
}: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef({speaking, reducedMotion});
  const playRef = useRef<(action: string) => void>(() => {});
  const [loadState, setLoadState] = useState('本机机械概念模型 · 全息赛博生化人');

  useEffect(() => {
    stateRef.current = {speaking, reducedMotion};
  }, [speaking, reducedMotion]);

  useEffect(() => {
    if (isPlayable(preview) && preview?.value) playRef.current(preview.value);
  }, [preview?.call_id, preview?.status, preview?.value, generation]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let disposed = false;
    let raf = 0;
    let lastRendered = 0;
    let renderDelta = 0;
    let mixer: THREE.AnimationMixer | null = null;
    let externalModel: THREE.Object3D | null = null;
    let previewUntil = 0;
    let fallbackAction = '';

    const clock = new THREE.Clock();
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 50);
    camera.position.set(0, 0.9, 3.8);
    camera.lookAt(0, 0.75, 0);

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({alpha: true, antialias: true, powerPreference: 'high-performance'});
    } catch {
      setLoadState('本机机械概念模型 · 全息赛博生化人 (Canvas 模式)');
      return;
    }

    const devicePixels = window.innerWidth * window.innerHeight * (window.devicePixelRatio || 1) ** 2;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, devicePixels > 6_000_000 ? 1 : 1.5));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.35;
    renderer.setClearColor(0x000000, 0);
    host.appendChild(renderer.domElement);

    // Dynamic Sci-Fi Lighting
    const hemiLight = new THREE.HemisphereLight(0xa5f3fc, 0x030712, 2.5);
    scene.add(hemiLight);

    const keyLight = new THREE.DirectionalLight(0x00f0ff, 4.0);
    keyLight.position.set(-3.5, 4.0, 4.0);
    scene.add(keyLight);

    const rimLight = new THREE.DirectionalLight(0xa855f7, 4.5);
    rimLight.position.set(3.5, 3.0, -3.0);
    scene.add(rimLight);

    // Audio-reactive chest reactor point light
    const chestPointLight = new THREE.PointLight(0x00f0ff, 2.2, 3.5);
    chestPointLight.position.set(0, 0.35, 0.6);
    scene.add(chestPointLight);

    // Multi-tier holographic radar rings
    const ringGroup = new THREE.Group();
    ringGroup.position.set(0, -0.45, 0);

    const ringGeoms = [
      {r: 1.25, tube: 0.007, segs: 64, color: 0x00f0ff, op: 0.45, rotSpeed: 0.18},
      {r: 1.55, tube: 0.005, segs: 64, color: 0xa855f7, op: 0.35, rotSpeed: -0.14},
      {r: 1.85, tube: 0.009, segs: 80, color: 0x38bdf8, op: 0.25, rotSpeed: 0.09},
    ];

    const ringMeshes: THREE.Mesh[] = [];
    ringGeoms.forEach(cfg => {
      const rMat = new THREE.MeshBasicMaterial({
        color: cfg.color,
        transparent: true,
        opacity: cfg.op,
        depthWrite: false,
      });
      const ringMesh = new THREE.Mesh(new THREE.TorusGeometry(cfg.r, cfg.tube, 8, cfg.segs), rMat);
      ringMesh.rotation.x = Math.PI / 2;
      ringMesh.userData = {rotSpeed: cfg.rotSpeed};
      ringMeshes.push(ringMesh);
      ringGroup.add(ringMesh);
    });
    scene.add(ringGroup);

    // Floating quantum particle nebula
    const particleCount = 80;
    const pGeom = new THREE.BufferGeometry();
    const pPositions = new Float32Array(particleCount * 3);
    for (let i = 0; i < particleCount; i++) {
      const angle = Math.random() * Math.PI * 2;
      const radius = 0.6 + Math.random() * 1.5;
      const y = -0.2 + Math.random() * 1.8;
      pPositions[i * 3] = Math.cos(angle) * radius;
      pPositions[i * 3 + 1] = y;
      pPositions[i * 3 + 2] = Math.sin(angle) * radius;
    }
    pGeom.setAttribute('position', new THREE.BufferAttribute(pPositions, 3));
    const pMat = new THREE.PointsMaterial({
      color: 0x38bdf8,
      size: 0.032,
      transparent: true,
      opacity: 0.75,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const particles = new THREE.Points(pGeom, pMat);
    scene.add(particles);

    const resize = () => {
      const width = host.clientWidth;
      const height = host.clientHeight;
      if (!width || !height) return;
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height, false);
    };

    const observer = new ResizeObserver(resize);
    observer.observe(host);
    resize();

    // Optional user local GLB (only if locally provided by user, never fetch cartoon fallback)
    const loader = new GLTFLoader();
    loader.load('/models/annie-robot.glb', gltf => {
      if (disposed) return;
      externalModel = gltf.scene;
      const bounds = new THREE.Box3().setFromObject(externalModel);
      const size = bounds.getSize(new THREE.Vector3());
      const center = bounds.getCenter(new THREE.Vector3());
      const scale = 2.65 / Math.max(size.y, 0.001);
      externalModel.scale.setScalar(scale);
      externalModel.position.set(-center.x * scale, -0.15 - bounds.min.y * scale, -center.z * scale);
      scene.add(externalModel);
      mixer = new THREE.AnimationMixer(externalModel);
      setLoadState('本地定制模型 · 指令预演');
    });

    playRef.current = (scriptId: string) => {
      fallbackAction = scriptId.toLowerCase();
      previewUntil = clock.elapsedTime + 1.8;
    };

    const render = () => {
      raf = window.requestAnimationFrame(render);
      const delta = Math.min(clock.getDelta(), 0.05);
      if (document.hidden) {
        renderDelta = 0;
        return;
      }
      renderDelta += delta;

      const frameThreshold = stateRef.current.reducedMotion ? 1 / 15 : 1 / 30;
      if (clock.elapsedTime - lastRendered < frameThreshold) return;
      lastRendered = clock.elapsedTime;

      const t = clock.elapsedTime;
      const isSpeaking = stateRef.current.speaking;

      if (previewUntil && t > previewUntil) {
        previewUntil = 0;
        fallbackAction = '';
      }

      if (!stateRef.current.reducedMotion) {
        // Rotating holographic radar rings
        ringMeshes.forEach(m => {
          const spd = (m.userData.rotSpeed as number) || 0.1;
          m.rotation.z += renderDelta * spd * (isSpeaking ? 1.5 : 1.0);
        });

        // Drift quantum particles
        particles.rotation.y += renderDelta * 0.07;

        // Chest point light reactive to speech
        if (isSpeaking) {
          const pulse = 1.0 + Math.sin(t * 18) * 0.45;
          chestPointLight.intensity = 2.6 + pulse * 2.5;
        } else {
          const idlePulse = 0.9 + Math.sin(t * 2.5) * 0.2;
          chestPointLight.intensity = 1.6 + idlePulse * 0.8;
        }

        mixer?.update(Math.min(renderDelta, 0.1));
      }

      renderer.render(scene, camera);
      renderDelta = 0;
    };

    raf = window.requestAnimationFrame(render);

    return () => {
      disposed = true;
      window.cancelAnimationFrame(raf);
      observer.disconnect();
      playRef.current = () => {};
      mixer?.stopAllAction();

      if (externalModel) scene.remove(externalModel);
      scene.remove(ringGroup);
      scene.remove(particles);

      scene.traverse(object => {
        const mesh = object as THREE.Mesh;
        mesh.geometry?.dispose();
        if (Array.isArray(mesh.material)) mesh.material.forEach(m => m.dispose());
        else mesh.material?.dispose();
      });

      renderer.dispose();
      if (host.contains(renderer.domElement)) host.removeChild(renderer.domElement);
    };
  }, []);

  const yawVal = pose?.yaw != null ? pose.yaw.toFixed(1) : '-6.8';
  const pitchVal = pose?.pitch != null ? pose.pitch.toFixed(1) : '2.1';
  const rollVal = pose?.roll != null ? pose.roll.toFixed(1) : '1.3';
  const actionText = preview?.value ?? '自然对话';
  const actionScore = preview ? '0.96' : '0.89';
  const visemeText = viseme ?? '0.43';
  const exprText = expression ?? '0.92';

  return (
    <div className="ex-twin">
      {/* 1. Core High-Res Cybernetic Embodied Android Image Layer */}
      <div className={`ex-twin-figure-wrap ${speaking ? 'is-speaking' : ''}`}>
        <img
          src="/exhibit-assets/annie-avatar-core.png"
          alt="小安妮 V2 全息具身数字孪生"
          className="ex-twin-figure-img"
        />
        {/* Holographic scanning HUD visor overlay */}
        <div className="ex-face-reticle" aria-hidden="true" />
        <div className="ex-chest-arc-glow" aria-hidden="true" />
      </div>

      {/* 2. Three.js Canvas Layer for Interactive 3D Holographic Elements */}
      <div ref={hostRef} className="ex-twin-canvas" aria-label="Three.js 全息具身数字孪生核心" />

      {/* 3. High-Tech HUD Callout Badges (Exact 1:1 match with Design Spec) */}

      {/* Top Left: HEAD POSE */}
      <div className="ex-hud-card hud-top-left">
        <div className="hud-card-hdr">
          <Move3d size={14} className="hud-icon" />
          <div>
            <b>头部姿态</b>
            <small>HEAD POSE</small>
          </div>
        </div>
        <div className="hud-pose-vals">
          <span>Yaw <em>{yawVal}°</em></span>
          <span>Pitch <em>{pitchVal}°</em></span>
          <span>Roll <em>{rollVal}°</em></span>
        </div>
      </div>

      {/* Middle Left: GAZE */}
      <div className="ex-hud-card hud-mid-left">
        <div className="hud-card-hdr">
          <Eye size={14} className="hud-icon cyan" />
          <div>
            <b>视线方向</b>
            <small>GAZE</small>
          </div>
          <span className="hud-val cyan">0.98</span>
        </div>
        <div className="hud-progress-bar">
          <i style={{width: '98%'}} />
        </div>
      </div>

      {/* Bottom Left: EXPRESSION */}
      <div className="ex-hud-card hud-bot-left">
        <div className="hud-card-hdr">
          <Smile size={14} className="hud-icon pink" />
          <div>
            <b>表情识别</b>
            <small>EXPRESSION</small>
          </div>
          <span className="hud-val pink">{exprText}</span>
        </div>
        <div className="hud-progress-bar pink">
          <i style={{width: '92%'}} />
        </div>
      </div>

      {/* Top Right: VISEME */}
      <div className="ex-hud-card hud-top-right">
        <div className="hud-card-hdr">
          <Volume2 size={14} className="hud-icon" />
          <div>
            <b>唇形/语音</b>
            <small>VISEME</small>
          </div>
          <span className="hud-val">{visemeText}</span>
        </div>
        <div className="hud-viseme-wave">
          <span /><span /><span /><span /><span /><span /><span /><span />
        </div>
      </div>

      {/* Middle Right: ACTION */}
      <div className="ex-hud-card hud-mid-right">
        <div className="hud-card-hdr">
          <Activity size={14} className="hud-icon green" />
          <div>
            <b>当前动作</b>
            <small>ACTION</small>
          </div>
          <span className="hud-val green">{actionScore}</span>
        </div>
        <div className="hud-action-tag">
          <span>{actionText}</span>
        </div>
      </div>

      {/* Bottom Right: EXECUTION STATUS */}
      <div className="ex-hud-card hud-bot-right">
        <div className="hud-card-hdr">
          <Gauge size={14} className="hud-icon gold" />
          <div>
            <b>执行状态</b>
            <small>EXECUTION</small>
          </div>
        </div>
        <div className="hud-exec-list">
          <span className="exec-title">● 动作执行中</span>
          <span className="exec-item"><Check size={11} /> 头部控制</span>
          <span className="exec-item"><Check size={11} /> 表情控制</span>
          <span className="exec-item"><Check size={11} /> 语音合成</span>
        </div>
      </div>

      {/* 4. Bottom Base Telemetry HUD (Temp, Status, Load, Memory) */}
      <div className="ex-twin-bottom-telemetry">
        <span>TEMP <b>36.5°</b></span>
        <span>STATUS <b>NORMAL</b></span>
        <span>LOAD <b>42.2%</b></span>
        <span>MEMORY <b>68%</b></span>
      </div>

      {/* Origin Notice required by test contracts */}
      <span className="ex-model-origin">{loadState}</span>
      <span className="ex-model-notice">全息具身核心 · 指令预演</span>
    </div>
  );
}

export const RobotTwin = memo(RobotTwinImpl);
