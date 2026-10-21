import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { App } from "./App";
import { AppStore, StoreContext } from "./store/store";

// One store for the page lifetime: it owns the /ws/events stream, the REST
// resync and the URL-hash selection. Everything it holds comes from the backend.
const store = new AppStore();
store.start();

const root = document.getElementById("root");
if (!root) throw new Error("#root missing from index.html");

createRoot(root).render(
  <StrictMode>
    <StoreContext.Provider value={store}>
      <App />
    </StoreContext.Provider>
  </StrictMode>,
);
