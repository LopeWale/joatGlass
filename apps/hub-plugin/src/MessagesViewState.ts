export type MessagesScreen =
  | 'conversations'
  | 'conversation'
  | 'message_detail'
  | 'review'
  | 'compose'
  | 'access'
  | 'setup'
  | 'empty'
  | 'read_only'
  | 'retry'
  | 'invalid';

export type MessagesRowAction = 'select_conversation' | 'read_message' | 'review' | 'retry' | 'none';

export interface MessagesViewState {
  screen: MessagesScreen;
  rowAction: MessagesRowAction;
  selectable: boolean;
}

/** Convert the Mac-authored generic wire title into a closed local state set. */
export function resolveMessagesViewState(
  title: string,
  wireSelectable: boolean,
): MessagesViewState {
  const normalized = title.trim().toUpperCase();
  if (normalized === 'MESSAGES') {
    return {
      screen: 'conversations',
      rowAction: wireSelectable ? 'select_conversation' : 'none',
      selectable: wireSelectable,
    };
  }
  if (normalized === 'MESSAGES · CONVERSATION') {
    return {
      screen: 'conversation',
      rowAction: wireSelectable ? 'read_message' : 'none',
      selectable: wireSelectable,
    };
  }
  if (normalized === 'MESSAGES · REPLY') {
    return { screen: 'conversation', rowAction: 'none', selectable: false };
  }
  if (normalized === 'MESSAGES · MESSAGE') {
    return { screen: 'message_detail', rowAction: 'none', selectable: false };
  }
  if (normalized === 'MESSAGES · REVIEW') {
    return { screen: 'review', rowAction: 'review', selectable: wireSelectable };
  }
  if (normalized === 'MESSAGES · COMPOSE') {
    return { screen: 'compose', rowAction: 'none', selectable: false };
  }
  if (normalized === 'MESSAGES · ACCESS') {
    return { screen: 'access', rowAction: 'retry', selectable: true };
  }
  if (normalized === 'MESSAGES · SETUP') {
    return { screen: 'setup', rowAction: 'retry', selectable: true };
  }
  if (normalized === 'MESSAGES · RETRY') {
    return { screen: 'retry', rowAction: 'retry', selectable: true };
  }
  if (normalized === 'MESSAGES · EMPTY') {
    return { screen: 'empty', rowAction: 'none', selectable: false };
  }
  if (normalized === 'MESSAGES · READ ONLY') {
    return { screen: 'read_only', rowAction: 'none', selectable: false };
  }
  return { screen: 'invalid', rowAction: 'none', selectable: false };
}
