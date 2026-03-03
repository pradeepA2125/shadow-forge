declare module "vscode" {
  export interface Disposable {
    dispose(): void;
  }

  export interface Memento {
    get<T>(key: string): T | undefined;
    update(key: string, value: unknown): Thenable<void>;
  }

  export interface ExtensionContext {
    subscriptions: Disposable[];
    workspaceState: Memento;
    extensionUri: Uri;
  }

  export interface WorkspaceFolder {
    readonly uri: Uri;
    readonly name: string;
    readonly index: number;
  }

  export interface Configuration {
    get<T>(key: string, defaultValue?: T): T;
  }

  export interface TextDocument {
    readonly uri: Uri;
  }

  export interface Uri {
    readonly fsPath: string;
    toString(skipEncoding?: boolean): string;
  }

  export namespace Uri {
    function file(path: string): Uri;
    function parse(value: string): Uri;
  }

  export interface Webview {
    html: string;
    readonly cspSource: string;
    onDidReceiveMessage(listener: (e: unknown) => unknown): Disposable;
    postMessage(message: unknown): Thenable<boolean>;
  }

  export interface WebviewPanel {
    readonly webview: Webview;
    title: string;
    reveal(viewColumn?: ViewColumn, preserveFocus?: boolean): void;
    onDidDispose(listener: () => unknown): Disposable;
    dispose(): void;
  }

  export enum ViewColumn {
    One = 1,
    Two = 2,
    Three = 3,
  }

  export interface InputBoxOptions {
    prompt?: string;
    placeHolder?: string;
    value?: string;
    validateInput?(value: string): string | null | undefined;
    ignoreFocusOut?: boolean;
  }

  export namespace window {
    function showInputBox(options?: InputBoxOptions): Thenable<string | undefined>;
    function showInformationMessage(message: string): Thenable<string | undefined>;
    function showWarningMessage(message: string): Thenable<string | undefined>;
    function showErrorMessage(message: string): Thenable<string | undefined>;
    function createWebviewPanel(
      viewType: string,
      title: string,
      showOptions: ViewColumn,
      options: { enableScripts?: boolean; retainContextWhenHidden?: boolean }
    ): WebviewPanel;
    function setStatusBarMessage(text: string, hideAfterTimeout?: number): Disposable;
  }

  export namespace workspace {
    const workspaceFolders: readonly WorkspaceFolder[] | undefined;
    function getConfiguration(section?: string): Configuration;
    function openTextDocument(
      uriOrOptions:
        | Uri
        | {
            language?: string;
            content?: string;
          }
    ): Thenable<TextDocument>;
  }

  export namespace commands {
    function registerCommand(
      command: string,
      callback: (...args: unknown[]) => unknown
    ): Disposable;
    function executeCommand<T = unknown>(command: string, ...rest: unknown[]): Thenable<T>;
  }
}
