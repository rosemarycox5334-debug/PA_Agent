/**
 * Application bootstrap. Mounts the Vue 3 app into #app and wires global
 * styles + router. Kept intentionally thin so views/components do the work.
 */
import { createApp } from 'vue';
import App from './App.vue';
import { router } from './router';
import './styles/tokens.css';
import './styles/global.css';

const app = createApp(App);
app.use(router);
app.mount('#app');
