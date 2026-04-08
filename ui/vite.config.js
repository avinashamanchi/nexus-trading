import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // base is set via VITE_BASE_URL env var:
  //   GitHub Pages:  VITE_BASE_URL=/nexus-trading/  (set in deploy.yml)
  //   Docker/local:  (empty → serves from /)
  base: process.env.VITE_BASE_URL || '/',
})
