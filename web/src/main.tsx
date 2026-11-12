import React from 'react';
import {createRoot} from 'react-dom/client';
import "@fontsource-variable/orbitron";
import {Workbench} from './workbench';
import './styles.css';
createRoot(document.getElementById('root')!).render(<React.StrictMode><Workbench/></React.StrictMode>);
