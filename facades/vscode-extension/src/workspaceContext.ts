import * as path from 'node:path';
import * as vscode from 'vscode';

export type JsonRecord = Record<string, unknown>;

const MAX_CONTEXT_CHARS = 30_000;
const MAX_DOCUMENT_SNAPSHOT_CHARS = 20_000;
const MAX_DIAGNOSTICS = 20;

export interface CapturedWorkspaceContext {
  attachments: JsonRecord[];
  extraContext: string;
  activeLabel: string;
}

function asRecord(value: unknown): JsonRecord {
  return value !== null && typeof value === 'object' ? value as JsonRecord : {};
}

function workspaceFolderForUri(uri: vscode.Uri | undefined): vscode.WorkspaceFolder | undefined {
  return uri ? vscode.workspace.getWorkspaceFolder(uri) : undefined;
}

function referenceUri(reference: vscode.ChatPromptReference): vscode.Uri | undefined {
  if (reference.value instanceof vscode.Uri) return reference.value;
  const nested = asRecord(reference.value).uri;
  return nested instanceof vscode.Uri ? nested : undefined;
}

function referenceRange(reference: vscode.ChatPromptReference): vscode.Range | undefined {
  const range = asRecord(reference.value).range;
  if (range instanceof vscode.Range) return range;
  return undefined;
}

function uniqueFolders(folders: Array<vscode.WorkspaceFolder | undefined>): vscode.WorkspaceFolder[] {
  const values = new Map<string, vscode.WorkspaceFolder>();
  for (const folder of folders) {
    if (folder) values.set(folder.uri.toString(), folder);
  }
  return [...values.values()];
}

function configuredFolder(root: string | undefined): vscode.WorkspaceFolder | undefined {
  if (!root) return undefined;
  const resolved = path.resolve(root);
  return vscode.workspace.workspaceFolders?.find((folder) => path.resolve(folder.uri.fsPath) === resolved);
}

export function contextualWorkspaceRoot(preferredRoot?: string): string | undefined {
  const active = workspaceFolderForUri(vscode.window.activeTextEditor?.document.uri);
  if (active) return active.uri.fsPath;
  const preferred = configuredFolder(preferredRoot);
  if (preferred) return preferred.uri.fsPath;
  const folders = vscode.workspace.workspaceFolders ?? [];
  return folders.length === 1 ? folders[0].uri.fsPath : undefined;
}

export async function resolveWorkspaceRoot(options: {
  references?: readonly vscode.ChatPromptReference[];
  preferredRoot?: string;
  promptIfAmbiguous?: boolean;
  forcePicker?: boolean;
} = {}): Promise<string | undefined> {
  const referenced = uniqueFolders(
    (options.references ?? []).map((reference) => workspaceFolderForUri(referenceUri(reference))),
  );
  if (referenced.length === 1 && !options.forcePicker) return referenced[0].uri.fsPath;

  const active = workspaceFolderForUri(vscode.window.activeTextEditor?.document.uri);
  if (!options.forcePicker && !referenced.length && active) return active.uri.fsPath;
  const preferred = configuredFolder(options.preferredRoot);
  if (!options.forcePicker && !referenced.length && preferred) return preferred.uri.fsPath;

  const candidates = referenced.length > 1
    ? referenced
    : [...(vscode.workspace.workspaceFolders ?? [])];
  if (candidates.length === 1 && !options.forcePicker) return candidates[0].uri.fsPath;
  if (!candidates.length || !options.promptIfAmbiguous) return undefined;
  const selected = await vscode.window.showQuickPick(candidates.map((folder) => ({
    folder,
    label: `$(folder) ${folder.name}`,
    description: folder.uri.fsPath,
  })), {
    title: 'Carpeta de trabajo para Baldr',
    placeHolder: 'Elegí dónde querés que trabaje esta conversación',
    ignoreFocusOut: true,
  });
  return selected?.folder.uri.fsPath;
}

function relativePath(root: string, uri: vscode.Uri): string | undefined {
  const relative = path.relative(path.resolve(root), path.resolve(uri.fsPath));
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return undefined;
  return relative.split(path.sep).join('/');
}

function diagnosticLabel(severity: vscode.DiagnosticSeverity): string {
  return ({
    [vscode.DiagnosticSeverity.Error]: 'error',
    [vscode.DiagnosticSeverity.Warning]: 'warning',
    [vscode.DiagnosticSeverity.Information]: 'information',
    [vscode.DiagnosticSeverity.Hint]: 'hint',
  } as Record<number, string>)[severity] ?? 'diagnostic';
}

export function captureWorkspaceContext(
  root: string,
  references: readonly vscode.ChatPromptReference[] = [],
): CapturedWorkspaceContext {
  const attachments: JsonRecord[] = [];
  const attachmentKeys = new Set<string>();
  const contextParts: string[] = [];
  let activeLabel = '';
  const addAttachment = (value: JsonRecord): void => {
    const key = `${String(value.kind ?? '')}:${String(value.path ?? '')}:${JSON.stringify(value.range ?? null)}`;
    if (!attachmentKeys.has(key)) {
      attachmentKeys.add(key);
      attachments.push(value);
    }
  };

  for (const reference of references) {
    const uri = referenceUri(reference);
    if (uri) {
      const relative = relativePath(root, uri);
      if (!relative) continue;
      const range = referenceRange(reference);
      addAttachment({
        kind: range ? 'selection' : 'file',
        label: range
          ? `${relative}:${range.start.line + 1}-${range.end.line + 1}`
          : relative,
        path: uri.fsPath,
        ...(range ? { range: { startLine: range.start.line + 1, endLine: range.end.line + 1 } } : {}),
      });
      continue;
    }
    if (typeof reference.value === 'string' && reference.value.trim()) {
      const label = reference.modelDescription?.trim() || reference.id || 'VS Code reference';
      contextParts.push(`Explicit VS Code reference (${label}):\n${reference.value.slice(0, 10_000)}`);
    }
  }

  const editor = vscode.window.activeTextEditor;
  const relative = editor ? relativePath(root, editor.document.uri) : undefined;
  if (editor && relative) {
    const selection = editor.selection;
    const range = selection.isEmpty ? undefined : {
      startLine: selection.start.line + 1,
      endLine: selection.end.line + 1,
    };
    activeLabel = range
      ? `${relative}:${range.startLine}-${range.endLine}`
      : relative;
    addAttachment({
      kind: range ? 'selection' : 'file',
      label: activeLabel,
      path: editor.document.uri.fsPath,
      language: editor.document.languageId,
      version: editor.document.version,
      dirty: editor.document.isDirty,
      ...(range ? { range } : {}),
    });
    contextParts.push(`Active VS Code editor: ${activeLabel} (language=${editor.document.languageId}, version=${editor.document.version}, dirty=${editor.document.isDirty}).`);
    if (range) {
      contextParts.push(`Current editor selection:\n${editor.document.getText(selection).slice(0, MAX_DOCUMENT_SNAPSHOT_CHARS)}`);
    } else if (editor.document.isDirty) {
      const snapshot = editor.document.getText();
      contextParts.push(
        `Unsaved editor snapshot${snapshot.length > MAX_DOCUMENT_SNAPSHOT_CHARS ? ' (truncated)' : ''}; preserve these user changes and do not assume the on-disk file is current:\n${snapshot.slice(0, MAX_DOCUMENT_SNAPSHOT_CHARS)}`,
      );
    }
    const diagnostics = vscode.languages.getDiagnostics(editor.document.uri).slice(0, MAX_DIAGNOSTICS);
    if (diagnostics.length) {
      contextParts.push([
        'Current diagnostics for the active editor:',
        ...diagnostics.map((diagnostic) => {
          const line = diagnostic.range.start.line + 1;
          const source = diagnostic.source ? ` ${diagnostic.source}` : '';
          return `- ${diagnosticLabel(diagnostic.severity)}${source} at line ${line}: ${diagnostic.message.slice(0, 500)}`;
        }),
      ].join('\n'));
    }
  }

  return {
    attachments: attachments.slice(0, 50),
    extraContext: contextParts.join('\n\n').slice(0, MAX_CONTEXT_CHARS),
    activeLabel,
  };
}
