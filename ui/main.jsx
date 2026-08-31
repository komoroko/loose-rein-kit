// Entry point. `index.html` carries an empty root and two globals the server stamps in; everything
// else on the page is rendered from here.

import { createRoot } from "react-dom/client";

import App from "./App.jsx";

createRoot(document.getElementById("root")).render(<App />);
