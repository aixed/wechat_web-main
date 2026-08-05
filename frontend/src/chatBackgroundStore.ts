const DATABASE_NAME = "wechat-web-chat-backgrounds";
const DATABASE_VERSION = 1;
const STORE_NAME = "backgrounds";

interface ChatBackgroundRecord {
  id: string;
  image: Blob;
  updatedAt: number;
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("IndexedDB is unavailable"));
      return;
    }

    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE_NAME)) {
        database.createObjectStore(STORE_NAME, { keyPath: "id" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("Unable to open chat background storage"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error || new Error("Chat background storage transaction aborted"));
    transaction.onerror = () => reject(transaction.error || new Error("Chat background storage transaction failed"));
  });
}

export async function getChatBackground(id: string): Promise<Blob | null> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(STORE_NAME, "readonly");
    const request = transaction.objectStore(STORE_NAME).get(id);
    const record = await new Promise<ChatBackgroundRecord | undefined>((resolve, reject) => {
      request.onsuccess = () => resolve(request.result as ChatBackgroundRecord | undefined);
      request.onerror = () => reject(request.error || new Error("Unable to read chat background"));
    });
    await transactionDone(transaction);
    return record?.image instanceof Blob ? record.image : null;
  } finally {
    database.close();
  }
}

export async function saveChatBackground(id: string, image: Blob): Promise<void> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).put({ id, image, updatedAt: Date.now() } satisfies ChatBackgroundRecord);
    await transactionDone(transaction);
  } finally {
    database.close();
  }
}

export async function deleteChatBackground(id: string): Promise<void> {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).delete(id);
    await transactionDone(transaction);
  } finally {
    database.close();
  }
}
