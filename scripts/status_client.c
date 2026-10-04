/* LimitSwitcher Status: Claude Code's status line, as a tiny native program.

   Claude Code starts the status line every few seconds for every open session, so this replaces
   the Python script (about 10 MB and 15 ms a run) with a program of about 1 MB and 1 ms. It does
   no parsing at all: it sends what Claude Code gives it on stdin to the running app (loopback,
   narrow token) and prints the app's answer, which is the finished, coloured line.

   Usage:  LimitSwitcher Status <state file> [anything: the marker that identifies our entry]

   The state file is plain text, written by the app (account_switcher/integrations.py):
       line 1: host (127.0.0.1)    line 2: port    line 3: token    line 4: cache file
   The app answers 200 with the line, 204 when it has nothing to show, 401/403 when it refuses (a token
   from before it restarted): then nothing is printed. If the app doesn't answer (busy, gone), the
   last line is printed for up to three minutes, like the Python script does (the cache file holds
   the time, a newline and the line).

   Used only when the person has no status line of their own (the app keeps the Python script for
   that: it has to run their command). Built for Windows (scripts\build_windows.ps1, with the name
   "LimitSwitcher Status") and macOS (scripts/build_mac.py); POSIX sockets and Winsock otherwise
   the same. */
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#define _WINSOCK_DEPRECATED_NO_WARNINGS /* inet_addr: the host is always the literal 127.0.0.1 */
#define _CRT_SECURE_NO_WARNINGS
#include <winsock2.h>
#include <windows.h>
typedef SOCKET sock_t;
#define NO_SOCKET INVALID_SOCKET
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
typedef int sock_t;
#define NO_SOCKET (-1)
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define IN_MAX 65536       /* what Claude Code hands over is a few KB */
#define OUT_MAX 65536
#define TIMEOUT_MS 600     /* the app is on this machine: it answers at once or it is busy */
#define CACHE_FOR 180      /* seconds the last line stands in while the app doesn't answer */
#define CACHE_REFRESH 60   /* an unchanged line is written to the cache again only this often */

static char input[IN_MAX];
static char reply[OUT_MAX];

/* ---------- files (paths are UTF-8; Windows converts) ---------- */
static FILE *open_file(const char *path, const char *mode) {
#ifdef _WIN32
    wchar_t wpath[1024], wmode[8];
    if (!MultiByteToWideChar(CP_UTF8, 0, path, -1, wpath, 1024)) return NULL;
    if (!MultiByteToWideChar(CP_UTF8, 0, mode, -1, wmode, 8)) return NULL;
    return _wfopen(wpath, wmode);
#else
    return fopen(path, mode);
#endif
}

static size_t read_all_file(const char *path, char *buffer, size_t size) {
    FILE *file = open_file(path, "rb");
    if (!file) return 0;
    size_t got = fread(buffer, 1, size - 1, file);
    fclose(file);
    buffer[got] = 0;
    return got;
}

/* ---------- stdin and stdout ---------- */
static size_t read_stdin(char *buffer, size_t size) {
    size_t used = 0;
#ifdef _WIN32
    HANDLE in = GetStdHandle(STD_INPUT_HANDLE);
    DWORD got;
    while (used < size && ReadFile(in, buffer + used, (DWORD)(size - used), &got, NULL) && got) used += got;
#else
    ssize_t got;
    while (used < size && (got = read(0, buffer + used, size - used)) > 0) used += (size_t)got;
#endif
    return used;
}

static void write_stdout(const char *text, size_t length) {
#ifdef _WIN32
    HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
    DWORD done;
    while (length) {
        if (!WriteFile(out, text, (DWORD)length, &done, NULL) || !done) return;
        text += done;
        length -= done;
    }
#else
    while (length) {
        ssize_t done = write(1, text, length);
        if (done <= 0) return;
        text += done;
        length -= (size_t)done;
    }
#endif
}

/* ---------- the app ---------- */
static int send_all(sock_t connection, const char *data, size_t length) {
    while (length) {
        int done = (int)send(connection, data, (int)length, 0);
        if (done <= 0) return 0;
        data += done;
        length -= (size_t)done;
    }
    return 1;
}

/* POST the input; returns the HTTP status (0: no answer) and sets *body / *body_length. */
static int ask(const char *host, int port, const char *token, const char *data, size_t length,
               char **body, size_t *body_length) {
    struct sockaddr_in address;
    memset(&address, 0, sizeof address);
    address.sin_family = AF_INET;
    address.sin_port = htons((unsigned short)port);
    address.sin_addr.s_addr = inet_addr(host);
    if (address.sin_addr.s_addr == INADDR_NONE) return 0;
    sock_t connection = socket(AF_INET, SOCK_STREAM, 0);
    if (connection == NO_SOCKET) return 0;
#ifdef _WIN32
    DWORD timeout = TIMEOUT_MS;
    setsockopt(connection, SOL_SOCKET, SO_RCVTIMEO, (const char *)&timeout, sizeof timeout);
    setsockopt(connection, SOL_SOCKET, SO_SNDTIMEO, (const char *)&timeout, sizeof timeout);
#else
    struct timeval timeout = {TIMEOUT_MS / 1000, (TIMEOUT_MS % 1000) * 1000};
    setsockopt(connection, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
    setsockopt(connection, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof timeout);
#endif
    int status = 0;
    size_t used = 0;
    char head[512];
    int head_length = snprintf(head, sizeof head,
        "POST /api/statusline-text HTTP/1.0\r\nHost: %s:%d\r\nAuthorization: Bearer %s\r\n"
        "Content-Type: application/json\r\nContent-Length: %lu\r\nConnection: close\r\n\r\n",
        host, port, token, (unsigned long)length);
    if (head_length <= 0 || head_length >= (int)sizeof head) goto done;
    if (connect(connection, (struct sockaddr *)&address, sizeof address) != 0) goto done;
    if (!send_all(connection, head, (size_t)head_length) || !send_all(connection, data, length)) goto done;
    for (int rounds = 0; rounds < 512 && used < OUT_MAX - 1; rounds++) {
        int got = (int)recv(connection, reply + used, (int)(OUT_MAX - 1 - used), 0);
        if (got <= 0) break;
        used += (size_t)got;
    }
    reply[used] = 0;
    char *split = strstr(reply, "\r\n\r\n");
    if (used < 12 || !split || strncmp(reply, "HTTP/1.", 7) != 0) goto done;
    status = atoi(reply + 9);
    *body = split + 4;
    *body_length = used - (size_t)(*body - reply);
done:
#ifdef _WIN32
    closesocket(connection);
#else
    close(connection);
#endif
    return status;
}

/* ---------- the state file: four lines ---------- */
static char *next_line(char **cursor) {
    char *start = *cursor;
    if (!start || !*start) return NULL;
    char *end = start;
    while (*end && *end != '\n') end++;
    if (*end) *(end++) = 0;
    size_t length = strlen(start);
    if (length && start[length - 1] == '\r') start[length - 1] = 0;
    *cursor = end;
    return start;
}

static void remember(const char *cache, const char *line, size_t length) {
    static char old[OUT_MAX + 32];
    size_t had = read_all_file(cache, old, sizeof old);
    char *newline = memchr(old, '\n', had);
    if (newline && (size_t)(had - (size_t)(newline + 1 - old)) == length &&
        memcmp(newline + 1, line, length) == 0 && (long)(time(NULL) - atol(old)) < CACHE_REFRESH)
        return; /* the same line again: nothing to write */
    FILE *file = open_file(cache, "wb");
    if (!file) return;
    fprintf(file, "%ld\n", (long)time(NULL));
    fwrite(line, 1, length, file);
    fclose(file);
}

static void recall(const char *cache) {
    static char old[OUT_MAX + 32];
    size_t had = read_all_file(cache, old, sizeof old);
    char *newline = memchr(old, '\n', had);
    if (!newline || (long)(time(NULL) - atol(old)) >= CACHE_FOR) return;
    write_stdout(newline + 1, had - (size_t)(newline + 1 - old));
}

static int run(const char *state_path) {
    static char state[2048];
    if (!read_all_file(state_path, state, sizeof state)) return 0; /* the app isn't set up: nothing to show */
    char *cursor = state;
    char *host = next_line(&cursor), *port = next_line(&cursor), *token = next_line(&cursor), *cache = next_line(&cursor);
    if (!host || !port || !token) return 0;
    size_t length = read_stdin(input, IN_MAX);
    if (length == IN_MAX) { /* more than any status line input: don't send half of it */
        if (cache) recall(cache);
        return 0;
    }
#ifdef _WIN32
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return 0;
#else
    signal(SIGPIPE, SIG_IGN);
#endif
    char *body = NULL;
    size_t body_length = 0;
    int status = ask(host, atoi(port), token, input, length, &body, &body_length);
    if (status == 200 && body_length) {
        write_stdout(body, body_length);
        if (cache) remember(cache, body, body_length);
    } else if ((status == 0 || status >= 500) && cache) {
        recall(cache); /* the app is busy or gone: the last line for a moment, not a blank one */
    }
    return 0; /* 204, 401, 403: nothing to show */
}

#ifdef _WIN32
int wmain(int argc, wchar_t **argv) {
    char path[2048];
    if (argc < 2 || !WideCharToMultiByte(CP_UTF8, 0, argv[1], -1, path, sizeof path, NULL, NULL)) return 0;
    return run(path);
}
#else
int main(int argc, char **argv) {
    return argc < 2 ? 0 : run(argv[1]);
}
#endif
