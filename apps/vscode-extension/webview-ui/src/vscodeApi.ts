interface VscodeApi {
  postMessage(msg: unknown): void;
}

declare function acquireVsCodeApi(): VscodeApi;

// acquireVsCodeApi() may only be called once per webview lifetime.
// In tests, window.acquireVsCodeApi is stubbed before this module loads.
const _api: VscodeApi =
  typeof acquireVsCodeApi === "function"
    ? acquireVsCodeApi()
    : { postMessage: () => {} };

export const vscode: VscodeApi = _api;
