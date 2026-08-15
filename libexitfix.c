#define _GNU_SOURCE
#include <stdlib.h>
#include <unistd.h>
#include <sys/syscall.h>

// 拦截 exit/_exit：当退出码为 1 时改为 0
// 背景：WPS SDK 的进程启动器（setsid+双fork）正常流程以 exit(1) 结束，
//       但 pywpsrpc 客户端把非 0 退出码判定为启动失败。
void _exit(int status) {
    syscall(SYS_exit_group, status == 1 ? 0 : status);
    for(;;);
}
void exit(int status) {
    syscall(SYS_exit_group, status == 1 ? 0 : status);
    for(;;);
}
