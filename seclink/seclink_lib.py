import weakref
import numpy as np
import time

from seclink._libseclink import ffi, lib

lib = ffi.dlopen(None)


def create_ctx(poldeg=4096, plain_mod=40961):
    def clear_ctx(ctx):
        lib.seclink_clear_ctx(ctx[0])

    assert plain_mod % (2 * poldeg) == 1, 'plain modulus must = 1 (mod 2n)'
    ctx = ffi.new('seclink_ctx_t *')
    lib.seclink_init_ctx(ctx, poldeg, plain_mod, ffi.NULL)
    weakref.finalize(ctx, clear_ctx, ctx)

    # See https://cffi.readthedocs.io/en/latest/using.html for why we
    # need to use "subscript 0".
    return ctx[0]


def keygen(ctx):
    # TODO: There must be a way to create a kind of memoryview over
    # the char array rather than copying
    def key_to_bytes(carr, nbytes):
        return b''.join(carr[0][j] for j in range(nbytes[0]))

    pkey = ffi.new('char **');
    plen = ffi.new('size_t *')
    skey = ffi.new('char **');
    slen = ffi.new('size_t *')
    gkey = ffi.new('char **');
    glen = ffi.new('size_t *')
    rkey = ffi.new('char **');
    rlen = ffi.new('size_t *')

    lib.seclink_keygen(ctx, pkey, plen, skey, slen, gkey, glen, rkey, rlen)

    pkey_ = key_to_bytes(pkey, plen)
    skey_ = key_to_bytes(skey, slen)
    gkey_ = key_to_bytes(gkey, glen)
    rkey_ = key_to_bytes(rkey, rlen)

    lib.seclink_clear_key(pkey[0])
    lib.seclink_clear_key(skey[0])
    lib.seclink_clear_key(gkey[0])
    lib.seclink_clear_key(rkey[0])

    return pkey_, skey_, gkey_, rkey_


def _clear_emat(emat):
    lib.seclink_clear_emat(emat[0])


def _encrypt_matrix(ctx, mat, pkey, encrypt_fn):
    # TODO: Use the other elements of __array_interface__ to handle
    # more complicated arrays, with offsets, striding, etc.

    assert mat.dtype == np.int64
    nrows, ncols = mat.__array_interface__['shape']
    mat_data, ro_flag = mat.__array_interface__['data']
    assert mat_data is not None

    mat_p = ffi.cast("int64_t *", mat_data)
    pkey_buf = ffi.from_buffer(pkey)

    emat = ffi.new('seclink_emat_t *')
    encrypt_fn(ctx, emat, mat_p, nrows, ncols, pkey_buf, len(pkey))
    weakref.finalize(emat, _clear_emat, emat)

    return emat[0]


def encrypt_left(ctx, mat, pkey):
    mat = mat.reshape(mat.shape, order='C')  # Ensure matrix is row-major
    return _encrypt_matrix(ctx, mat, pkey, lib.seclink_encrypt_left)


def encrypt_right(ctx, mat, pkey):
    mat = mat.reshape(mat.shape, order='F')  # Ensure matrix is column-major
    return _encrypt_matrix(ctx, mat, pkey, lib.seclink_encrypt_right)


def matmul(ctx, lmat, rmat, gkeys):
    prod = ffi.new('seclink_emat_t *')
    gkeys_buf = ffi.from_buffer(gkeys)
    lib.seclink_multiply(ctx, prod, lmat, rmat, gkeys_buf, len(gkeys))
    weakref.finalize(prod, _clear_emat, prod)

    return prod[0]


def _emat_shape(emat):
    nrows = ffi.new('size_t *')
    ncols = ffi.new('size_t *')
    lib.seclink_emat_shape(nrows, ncols, emat)
    return nrows[0], ncols[0]


def decrypt(ctx, inmat, skey):
    nrows, ncols = _emat_shape(inmat)
    outmat = np.empty(shape=(nrows, ncols), dtype=np.int64)
    outmat_data, ro_flag = outmat.__array_interface__['data']
    assert outmat_data is not None
    assert ro_flag is False

    outmat_p = ffi.cast("int64_t *", outmat_data)
    skey_buf = ffi.from_buffer(skey)
    lib.seclink_decrypt(ctx, outmat_p, nrows, ncols, inmat, skey_buf, len(skey))

    return outmat


def run_test(
        left_rows=2048,  # maximum allowable at the moment
        left_cols=512,  # 'BF length'
        right_cols=2,
        maxval=2,  # maximum vector element value + 1; 2 => bit vectors
        log=lambda *args, **kwargs: None):
    assert left_rows > 0 and left_cols > 0 and right_cols > 0, 'dimensions must be positive'
    right_rows = left_cols

    poldeg = 4096  # must be 4096 or 2048
    plain_mod = 40961  # must be = 1 (mod 2*poldeg) and < 2^60

    # These restriction will be lifted eventually
    assert left_rows <= poldeg / 2 and left_cols <= poldeg / 2, 'dimensions too big'
    if left_cols & (left_cols - 1) != 0:
        print('WARN: left_cols/right_rows sometimes fails if not = 2^n for some n')

    # log variant that doesn't print a new line, but still flushes output
    log_ = lambda *args: log(*args, end='', flush=True)
    # convenience for printing the elapsed time
    log_t = lambda t: log('{:0.1f}ms'.format(1000 * (time.time() - t)))

    log_('creating context... ')
    t = time.time()
    ctx = create_ctx(poldeg, plain_mod)
    log_t(t)

    t = time.time()
    log_('generating keys... ')
    pk, sk, gk, _ = keygen(ctx)
    log_t(t)

    # left is a random matrix with shape left_rows x left_cols
    left_size = left_rows * left_cols
    left = np.random.randint(maxval, size=left_size, dtype=np.int64)
    left = left.reshape(left_rows, left_cols)

    # right is a random matrix with shape left_cols x right_cols
    right_size = right_rows * right_cols
    right = np.random.randint(maxval, size=right_size, dtype=np.int64)
    right = right.reshape(right_rows, right_cols, order='F')

    log_('encrypting {}x{} left matrix... '.format(left_rows, left_cols))
    t = time.time()
    eLeft = encrypt_left(ctx, left, pk)
    log_t(t)

    log_('encrypting {}x{} right matrix... '.format(right_rows, right_cols))
    t = time.time()
    eRight = encrypt_right(ctx, right, pk)
    log_t(t)

    log_('multiplying encrypted matrices... ')
    t = time.time()
    prod = matmul(ctx, eLeft, eRight, gk)
    log_t(t)

    log_('decrypting {}x{} product matrix... '.format(left_rows, right_cols))
    t = time.time()
    out = decrypt(ctx, prod, sk)
    log_t(t)

    expected = (left @ right) % t
    result = (out == expected)
    okay = result.all()
    log('result is correct? ', okay)

    if not okay:
        log([(i, j) for i in range(left_rows) for j in range(right_cols) if not result[i][j]])

    return okay
