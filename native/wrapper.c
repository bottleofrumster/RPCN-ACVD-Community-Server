#include <stddef.h>
#include <string.h>

#include "aes_modes/cwc.h"

int acvd_cwc_encrypt_aad(
    const unsigned char *key,
    size_t key_len,
    const unsigned char *iv,
    size_t iv_len,
    const unsigned char *aad,
    size_t aad_len,
    const unsigned char *input,
    size_t input_len,
    unsigned char *output,
    unsigned char *tag,
    size_t tag_len)
{
    cwc_ctx context[1];
    if (cwc_init_and_key(key, key_len, context) != RETURN_GOOD)
        return RETURN_ERROR;
    if (input_len)
        memcpy(output, input, input_len);
    const int result = cwc_encrypt_message(
        iv, iv_len, aad, aad_len, output, input_len, tag, tag_len, context);
    cwc_end(context);
    return result;
}

int acvd_cwc_decrypt_aad(
    const unsigned char *key,
    size_t key_len,
    const unsigned char *iv,
    size_t iv_len,
    const unsigned char *aad,
    size_t aad_len,
    const unsigned char *input,
    size_t input_len,
    unsigned char *output,
    const unsigned char *tag,
    size_t tag_len)
{
    cwc_ctx context[1];
    if (cwc_init_and_key(key, key_len, context) != RETURN_GOOD)
        return RETURN_ERROR;
    if (input_len)
        memcpy(output, input, input_len);
    const int result = cwc_decrypt_message(
        iv, iv_len, aad, aad_len, output, input_len, tag, tag_len, context);
    cwc_end(context);
    return result;
}

int acvd_cwc_encrypt(
    const unsigned char *key,
    size_t key_len,
    const unsigned char *iv,
    size_t iv_len,
    const unsigned char *input,
    size_t input_len,
    unsigned char *output,
    unsigned char *tag,
    size_t tag_len)
{
    return acvd_cwc_encrypt_aad(
        key, key_len, iv, iv_len, iv, iv_len, input, input_len, output, tag, tag_len);
}

int acvd_cwc_decrypt(
    const unsigned char *key,
    size_t key_len,
    const unsigned char *iv,
    size_t iv_len,
    const unsigned char *input,
    size_t input_len,
    unsigned char *output,
    const unsigned char *tag,
    size_t tag_len)
{
    return acvd_cwc_decrypt_aad(
        key, key_len, iv, iv_len, iv, iv_len, input, input_len, output, tag, tag_len);
}
