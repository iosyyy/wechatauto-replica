/* 合成目标：只包含公开固定测试字节，不访问微信或真实数据库。 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static void no_op(void) {}
static unsigned char candidate[32];
static unsigned char salt[16];
static unsigned char output[32];
static unsigned char provider[128];
static unsigned char codec[160];
static unsigned char password[32];
extern void fixture_boundary(void *codec, void *password, void *candidate);

/* 精确匹配的指令前后仅准备测试用寄存器与栈；HMAC 签名位于不可达区域。 */
__asm__(
    ".text\n.global fixture_boundary\n.type fixture_boundary,@function\n"
    "fixture_boundary:\n"
    "push %r14\n push %rbx\n sub $8,%rsp\n"
    "mov %rdi,%r14\n mov %rsi,%rbx\n mov $16,%r9d\n mov $32,%r10\n"
    ".byte 0x49,0x8b,0x46,0x78,0x49,0x8b,0xbe,0x80,0,0,0,0x45,0x8b,0x5e,0x04,0x41,0x8b,0x76,0x34,0x8b,0x4b,0x04,0x4d,0x8b,0x46,0x48,0x48,0x83,0xec,0x08,0xff,0x73,0x08,0x41,0x52,0x41,0x53,0xff,0x50,0x30\n"
    "add $32,%rsp\n add $8,%rsp\n pop %rbx\n pop %r14\n ret\n"
    ".byte 0x49,0x8b,0x46,0x78,0x49,0x8b,0xbe,0x80,0,0,0,0x41,0x8b,0x76,0x34,0x45,0x8b,0x56,0x08,0x41,0x8b,0x4e,0x10,0x4d,0x8b,0x46,0x50,0x48,0x8b,0x53,0x08,0x48,0x83,0xec,0x08,0xff,0x73,0x10,0x51,0x41,0x52,0xff,0x50,0x30\n"
    ".size fixture_boundary,.-fixture_boundary\n"
);

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    for (int i = 0; i < 32; ++i) candidate[i] = (unsigned char)i;
    void (*function)(void) = no_op;
    void *pointer = provider;
    memcpy(provider + 0x30, &function, sizeof function);
    memcpy(codec + 0x78, &pointer, sizeof pointer);
    pointer = salt; memcpy(codec + 0x48, &pointer, sizeof pointer);
    uint32_t value = 256000; memcpy(codec + 4, &value, 4);
    value = 2; memcpy(codec + 0x34, &value, 4);
    value = 32; memcpy(password + 4, &value, 4);
    pointer = output; memcpy(password + 8, &pointer, sizeof pointer);
    puts("ready"); fflush(stdout);
    for (;;) {
        usleep(10000);
        if (access(argv[1], F_OK) == 0) { unlink(argv[1]); fixture_boundary(codec, password, candidate); }
    }
}
