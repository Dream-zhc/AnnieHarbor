import React from 'react';
import {createRoot} from 'react-dom/client';
import "@fontsource-variable/orbitron";
import "@fontsource/rajdhani";
import {App} from './app';
import './styles.css';
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
