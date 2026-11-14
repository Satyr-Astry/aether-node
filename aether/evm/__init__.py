"""EVM 工具：为链上结算构造可广播的交易载荷。

全部自实现，不依赖 web3/eth-account/solc：
  keccak  原始 Keccak-256（以太坊用，不是 SHA-3）
  rlp     Recursive Length Prefix 编码
  ecdsa   secp256k1 ECDSA + RFC6979 确定性 nonce + 地址推导
  tx      EIP-155 交易签名与原始交易序列化
  abi     Solidity 函数调用的 calldata 编码

各模块都用官方测试向量验证过，见 tests/test_evm.py。
"""
