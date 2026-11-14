// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IAetherToken {
    function mint(address to, uint256 value) external;
    function transfer(address to, uint256 value) external returns (bool);
}

/// @title AetherDistributor
/// @notice 按期的 Merkle root 发放。链上只存 root，节点自己带证明来领。
/// @dev    这样链上开销与节点数量无关：一千个节点和十个节点都只是存一个 32 字节 root。
///
///         叶子 = sha256(abi.encodePacked(address recipient, uint256 amount))，
///         注意只含地址与数额，不含点数和 epoch——点数是链下的记账口径，
///         一旦进入叶子就无法用合约重算（浮点在链上没有表示）。
///
///         内部节点 = sha256(abi.encodePacked(min(a,b), max(a,b)))，与 Python 侧一致。
contract AetherDistributor {
    IAetherToken public immutable token;
    address public owner;

    /// epoch => root
    mapping(uint256 => bytes32) public roots;
    /// epoch => 该期总额（用于核对，不参与验证）
    mapping(uint256 => uint256) public totals;
    /// epoch => recipient => 是否已领取
    mapping(uint256 => mapping(address => bool)) public claimed;

    event RootSet(uint256 indexed epoch, bytes32 root, uint256 total);
    event Claimed(uint256 indexed epoch, address indexed recipient, uint256 amount);

    error NotOwner();
    error RootAlreadySet();
    error NothingToClaim();
    error AlreadyClaimed();
    error BadProof();

    constructor(address token_) {
        token = IAetherToken(token_);
        owner = msg.sender;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    /// @notice 设定某期的发放根。同一期只能设定一次。
    function setRoot(uint256 epoch, bytes32 root, uint256 total) external onlyOwner {
        if (roots[epoch] != bytes32(0)) revert RootAlreadySet();
        roots[epoch] = root;
        totals[epoch] = total;
        emit RootSet(epoch, root, total);
    }

    /// @notice 节点凭证明领取本期的代币。
    function claim(uint256 epoch, uint256 amount, bytes32[] calldata proof) external {
        if (amount == 0) revert NothingToClaim();
        bytes32 root = roots[epoch];
        if (root == bytes32(0)) revert NothingToClaim();
        if (claimed[epoch][msg.sender]) revert AlreadyClaimed();

        bytes32 leaf = leafOf(msg.sender, amount);
        if (!verifyLeaf(root, leaf, proof)) revert BadProof();

        claimed[epoch][msg.sender] = true;
        token.mint(msg.sender, amount);
        emit Claimed(epoch, msg.sender, amount);
    }

    function leafOf(address recipient, uint256 amount) public pure returns (bytes32) {
        return sha256(abi.encodePacked(recipient, amount));
    }

    function verifyLeaf(bytes32 root, bytes32 leaf, bytes32[] calldata proof)
        public
        pure
        returns (bool)
    {
        bytes32 h = leaf;
        for (uint256 i = 0; i < proof.length; i++) {
            bytes32 s = proof[i];
            h = h < s ? sha256(abi.encodePacked(h, s)) : sha256(abi.encodePacked(s, h));
        }
        return h == root;
    }
}
