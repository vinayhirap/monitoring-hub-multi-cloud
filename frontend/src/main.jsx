// monitoring-hub/frontend/src/main.jsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/tokens.css'
import './index.css'
import './components/ui/ui.css'
import './styles/a11y-contrast.css'
import './api/httpDefaults.js'
import App from './App.jsx'
import ErrorBoundary from './components/ErrorBoundary.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ErrorBoundary root>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
