// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title AetherToken
/// @notice 最小 ERC-20 实现，不引入 OpenZeppelin 以保持自包含。
/// @dev    增发只允许 owner 调用，且受硬上限约束。经济参数（上限、曲线）
///         不在合约里硬编码决策——那属于治理，这里只保证接口标准。
contract AetherToken {
    string public name = "Aether Compute Token";
    string public symbol = "AETH";
    uint8 public decimals = 18;

    uint256 public totalSupply;
    uint256 public immutable cap;
    address public owner;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    error NotOwner();
    error CapExceeded();
    error InsufficientBalance();
    error InsufficientAllowance();
    error ZeroAddress();

    constructor(uint256 cap_) {
        if (cap_ == 0) revert CapExceeded();
        cap = cap_;
        owner = msg.sender;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    function transfer(address to, uint256 value) external returns (bool) {
        _transfer(msg.sender, to, value);
        return true;
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            if (allowed < value) revert InsufficientAllowance();
            allowance[from][msg.sender] = allowed - value;
        }
        _transfer(from, to, value);
        return true;
    }

    /// @notice 增发。只给 owner，且不能突破上限。
    function mint(address to, uint256 value) external onlyOwner {
        if (totalSupply + value > cap) revert CapExceeded();
        totalSupply += value;
        balanceOf[to] += value;
        emit Transfer(address(0), to, value);
    }

    function _transfer(address from, address to, uint256 value) internal {
        if (to == address(0)) revert ZeroAddress();
        uint256 bal = balanceOf[from];
        if (bal < value) revert InsufficientBalance();
        unchecked {
            balanceOf[from] = bal - value;
        }
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }
}
