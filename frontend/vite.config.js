import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react-swc'

export default defineConfig({
  plugins: [react()],
  build: {
    // ONE stylesheet for the whole app, loaded up front (as it was before pages were split into separate JS chunks).
    //
    // With the default (cssCodeSplit: true) each page's .css only downloads with that page's JavaScript. The pages share
    // class names across files (service-detail pages use AccountDetail.css; Overview uses .btn-refresh from Alerts.css;
    // Reports uses a button style from Compliance/Topology), so a page opened directly or refreshed rendered largely
    // unstyled: the EC2 / EBS service pages and the Overview Refresh button looked like raw HTML. The whole stylesheet is
    // ~35 KB gzipped, so splitting it saved almost nothing and broke the look. Keep this false.
    cssCodeSplit: false,
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
        '/admin': {
         target: 'http://localhost:8000',
         changeOrigin: true,
       },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
})