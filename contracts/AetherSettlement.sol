// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title AetherSettlement
/// @notice 只做两件事：锚定结算批次的 Merkle root，以及让任何人用证明验证某条账目。
///         发放逻辑与代币发行不在本合约里——它只负责"这批账目是某个时刻被提交过的"。
/// @dev    Merkle 用 sha256(bytes32 a, bytes32 b)，与 Python 侧
///         `hashlib.sha256(bytes.fromhex(a) + bytes.fromhex(b))` 一致。
///         叶子在排序后两两拼接，所以证明路径与顺序无关。
contract AetherSettlement {
    address public owner;

    /// epoch => merkle root；同一 epoch 只允许提交一次
    mapping(uint256 => bytes32) public batchRoots;
    /// epoch => 该批次声明的总额（单位由使用方约定）
    mapping(uint256 => uint256) public batchAmounts;

    event BatchCommitted(uint256 indexed epoch, bytes32 root, uint256 amount, address indexed submitter);

    error NotOwner();
    error AlreadyCommitted();
    error EmptyRoot();

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    /// @notice 提交某个 epoch 的结算根。
    function commitBatch(bytes32 root, uint256 epoch, uint256 amount) external onlyOwner {
        if (root == bytes32(0)) revert EmptyRoot();
        if (batchRoots[epoch] != bytes32(0)) revert AlreadyCommitted();
        batchRoots[epoch] = root;
        batchAmounts[epoch] = amount;
        emit BatchCommitted(epoch, root, amount, msg.sender);
    }

    /// @notice 验证一片叶子是否属于某个 root。
    /// @param leaf sha256(req_id|peer|model|in|out|amount) 的 32 字节结果
    /// @param proof 兄弟路径，由 Python 侧 merkle_proof 生成
    function verifyLeaf(bytes32 root, bytes32 leaf, bytes32[] calldata proof)
        public
        pure
        returns (bool)
    {
        bytes32 h = leaf;
        for (uint256 i = 0; i < proof.length; i++) {
            bytes32 sibling = proof[i];
            h = h < sibling ? sha256(abi.encodePacked(h, sibling)) : sha256(abi.encodePacked(sibling, h));
        }
        return h == root;
    }
}
