#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <errno.h>
#include <stdarg.h>
#include <sys/select.h>
#include <poll.h>
#include <time.h>
#include <execinfo.h>

// 用 FIFO 模拟 POSIX mqueue：mq_open 返回真实 fd（支持 poll），消息带长度前缀
typedef int mqd_t;
struct mq_attr { long mq_flags, mq_maxmsg, mq_msgsize, mq_curmsgs; };

static void name2path(const char *name, char *out, size_t n) {
    const char *p = name;
    if (*p == '/') p++;
    snprintf(out, n, "/tmp/mqsim-%s", p);
}

mqd_t mq_open(const char *name, int oflag, ...) {
    mode_t mode = 0644;
    va_list ap;
    va_start(ap, oflag);
    if (oflag & O_CREAT) {
        va_arg(ap, mode_t);
        va_arg(ap, struct mq_attr *);
    }
    va_end(ap);
    if (!name) { errno = EINVAL; return -1; }
    char path[512];
    name2path(name, path, sizeof(path));
    fprintf(stderr, "[mqsim] mq_open(%s, oflag=0x%x)\n", name, oflag);
    { void *bt[10]; int n = backtrace(bt, 10); char **s = backtrace_symbols(bt, n);
      for (int i = 1; i < n && i < 4; i++) fprintf(stderr, "  [bt] %s\n", s[i]); free(s); }
    // 总是创建（WPS 端 O_WRONLY 连接时也需要存在）
    mkfifo(path, 0600);
    // O_RDWR 打开避免阻塞等待对端
    int fd = open(path, O_RDWR | O_NONBLOCK);
    if (fd < 0) { errno = EMFILE; return -1; }
    // 清掉 O_NONBLOCK（读时阻塞等消息）
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags & ~O_NONBLOCK);
    errno = 0;  // 清 errno：避免客户端检查残留 errno
    fprintf(stderr, "[mqsim] mq_open => fd=%d (errno=%d)\n", fd, errno);
    return fd;
}

int mq_close(mqd_t mq) { return close(mq); }

int mq_unlink(const char *name) {
    char path[512];
    name2path(name, path, sizeof(path));
    return unlink(path);
}

int mq_send(mqd_t mq, const char *msg_ptr, size_t msg_len, unsigned msg_prio) {
    (void)msg_prio;
    fprintf(stderr, "[mqsim] mq_send(fd=%d, len=%zu)\n", mq, msg_len);
    char lenbuf[4];
    // 4 字节大端长度
    lenbuf[0] = (char)((msg_len >> 24) & 0xff);
    lenbuf[1] = (char)((msg_len >> 16) & 0xff);
    lenbuf[2] = (char)((msg_len >> 8) & 0xff);
    lenbuf[3] = (char)(msg_len & 0xff);
    // 写长度
    while (write(mq, lenbuf, 4) < 0) {
        if (errno == EINTR) continue;
        if (errno == EAGAIN) { usleep(1000); continue; }
        return -1;
    }
    size_t done = 0;
    while (done < msg_len) {
        ssize_t w = write(mq, msg_ptr + done, msg_len - done);
        if (w < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN) { usleep(1000); continue; }
            return -1;
        }
        done += w;
    }
    return 0;
}

ssize_t mq_receive(mqd_t mq, char *msg_ptr, size_t msg_len, unsigned *msg_prio) {
    if (msg_prio) *msg_prio = 0;
    fprintf(stderr, "[mqsim] mq_receive(fd=%d, len=%zu)\n", mq, msg_len);
    char lenbuf[4];
    ssize_t got = 0;
    while (got < 4) {
        ssize_t r = read(mq, lenbuf + got, 4 - got);
        if (r < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (r == 0) { usleep(1000); continue; }  // 无数据则等待
        got += r;
    }
    size_t len = ((unsigned char)lenbuf[0] << 24) | ((unsigned char)lenbuf[1] << 16) |
                 ((unsigned char)lenbuf[2] << 8) | (unsigned char)lenbuf[3];
    if (len > msg_len) len = msg_len;
    got = 0;
    while (got < len) {
        ssize_t r = read(mq, msg_ptr + got, len - got);
        if (r < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (r == 0) { usleep(1000); continue; }
        got += r;
    }
    return got;
}

int mq_getattr(mqd_t mq, struct mq_attr *attr) {
    fprintf(stderr, "[mqsim] mq_getattr(fd=%d)\n", mq);
    if (!attr) { errno = EFAULT; return -1; }
    attr->mq_flags = 0;
    attr->mq_maxmsg = 10;
    attr->mq_msgsize = 108;
    attr->mq_curmsgs = 0;
    return 0;
}

int mq_setattr(mqd_t mq, const struct mq_attr *newattr, struct mq_attr *oldattr) {
    if (oldattr) mq_getattr(mq, oldattr);
    return 0;
}

// mq_timedreceive：poll 等待数据直到超时
ssize_t mq_timedreceive(mqd_t mq, char *msg_ptr, size_t msg_len, unsigned *msg_prio, const struct timespec *abs_timeout) {
    struct pollfd pfd = {mq, POLLIN, 0};
    for (;;) {
        struct timespec now;
        clock_gettime(CLOCK_REALTIME, &now);
        long ms = (abs_timeout->tv_sec - now.tv_sec) * 1000 + (abs_timeout->tv_nsec - now.tv_nsec) / 1000000;
        if (ms < 0) { errno = ETIMEDOUT; return -1; }
        int r = poll(&pfd, 1, ms);
        if (r > 0) return mq_receive(mq, msg_ptr, msg_len, msg_prio);
        if (r == 0) { errno = ETIMEDOUT; return -1; }
        if (errno == EINTR) continue;
        return -1;
    }
}

int mq_timedsend(mqd_t mq, const char *msg_ptr, size_t msg_len, unsigned msg_prio, const struct timespec *abs_timeout) {
    (void)abs_timeout;
    return mq_send(mq, msg_ptr, msg_len, msg_prio);
}
