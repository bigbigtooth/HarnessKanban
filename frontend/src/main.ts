import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { initTheme } from './stores/theme'
import './styles.css'

// 主题必须在挂载前写进 <html>，避免首帧闪烁。
initTheme()

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
