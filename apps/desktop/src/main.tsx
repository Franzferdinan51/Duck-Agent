import React from 'react'
import { createRoot } from 'react-dom/client'
import { DuckAgentApp } from './app'
import './styles.css'
import './app/settings/settings.css'

const root = document.getElementById('root')
if (!root) throw new Error('Duck Agent root element was not found')

createRoot(root).render(
  <React.StrictMode>
    <DuckAgentApp />
  </React.StrictMode>,
)
