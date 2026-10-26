import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { devReadBridge } from './scripts/dev-read-bridge'
export default defineConfig({plugins:[react(),devReadBridge()],server:{host:'127.0.0.1',port:1432,strictPort:true},clearScreen:false})
