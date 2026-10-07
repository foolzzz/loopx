import { renderToString } from "react-dom/server";
import { App } from "./App";
import { pageMetadata, siteUrl } from "./page-metadata";

export { pageMetadata, siteUrl };

export function render() {
  return renderToString(<App />);
}
